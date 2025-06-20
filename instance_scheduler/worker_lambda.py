import json
import os
import boto3
import logging
from datetime import datetime, timezone
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

table_name = os.environ['DDB_TABLE_NAME']
org_member_role_name = os.environ['ORG_MEMBER_ROLE_NAME']
sqs_queue_url = os.environ['SQS_QUEUE_URL']
event_bus_name = os.environ['EVENT_BUS_NAME']

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(table_name)

sqs = boto3.client('sqs')
events = boto3.client('events')
sts = boto3.client('sts')

def assume_role(account_id, role_name):
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    try:
        credentials = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName="CrossAccountSession"
        )['Credentials']
        return boto3.client(
            'ec2',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
    except ClientError as e:
        logger.error(f"Error assuming role into {account_id}: {e}")
        return None

def fetch_instance_tags(ec2_client, instance_id):
    try:
        tags_response = ec2_client.describe_tags(Filters=[
            {'Name': 'resource-id', 'Values': [instance_id]}
        ])
        tags = tags_response.get('Tags', [])
        return {tag['Key']: tag['Value'] for tag in tags}
    except ClientError as e:
        logger.error(f"Error fetching tags for {instance_id}: {e}")
        return {}

def normalize_tags(tag_map):
    if not isinstance(tag_map, dict):
        return {}
    return {str(k): str(v) for k, v in tag_map.items()}

def handle_instance_state_change(message):
    detail = message.get('detail', {})
    instance_id = detail.get('instance-id')
    account_id = message.get('account')

    if not instance_id or not account_id:
        logger.warning("Missing instance-id or account-id in event detail")
        return

    ec2_client = assume_role(account_id, org_member_role_name)
    if not ec2_client:
        return

    tag_map = fetch_instance_tags(ec2_client, instance_id)
    if not tag_map:
        return

    # Tag auto-correction logic
    has_name_tag = any(k.startswith('Name') for k in tag_map)
    has_platform_tag = 'platform' in tag_map

    if has_name_tag and not has_platform_tag:
        new_tags = [
            {'Key': 'platform', 'Value': 'default-platform'},
            {'Key': 'AutoStart', 'Value': '08:00'},
            {'Key': 'AutoStop', 'Value': '20:00'}
        ]
        try:
            ec2_client.create_tags(Resources=[instance_id], Tags=new_tags)
            logger.info(f"Added default tags to {instance_id}")
            for t in new_tags:
                tag_map[t['Key']] = t['Value']
        except ClientError as e:
            logger.error(f"Failed to create default tags for {instance_id}: {e}")
            return

    # Store/update in DynamoDB with normalized flat tags
    item = {
        'InstanceId': instance_id,
        'AccountId': account_id,
        'Tags': normalize_tags(tag_map),
        'LastUpdated': datetime.now(timezone.utc).isoformat(),
        'State': detail.get('state', 'unknown')
    }
    table.put_item(Item=item)
    logger.info(f"Upserted instance {instance_id} with state-change event")

def handle_tag_change_event(message):
    detail = message.get('detail', {})
    instance_id = detail.get('resource-id') or detail.get('instance-id')
    account_id = message.get('account')

    if not instance_id or not account_id:
        logger.warning("Missing instance-id or account-id in tag change event")
        return

    ec2_client = assume_role(account_id, org_member_role_name)
    if not ec2_client:
        return

    tag_map = fetch_instance_tags(ec2_client, instance_id)
    if not tag_map:
        return

    # Store/update in DynamoDB with normalized flat tags
    item = {
        'InstanceId': instance_id,
        'AccountId': account_id,
        'Tags': normalize_tags(tag_map),
        'LastUpdated': datetime.now(timezone.utc).isoformat(),
        'State': 'unknown'
    }
    table.put_item(Item=item)
    logger.info(f"Upserted instance {instance_id} from tag change event")

def refresh_stale_tag_data():
    logger.info("Running scheduled DynamoDB record refresh")
    try:
        response = table.scan()
        items = response.get('Items', [])

        for item in items:
            instance_id = item.get('InstanceId')
            account_id = item.get('AccountId')
            stored_tags = normalize_tags(item.get('Tags', {}))
            stored_state = item.get('State')

            if not instance_id or not account_id:
                continue

            ec2_client = assume_role(account_id, org_member_role_name)
            if not ec2_client:
                continue

            # Fetch latest tags
            latest_tags = fetch_instance_tags(ec2_client, instance_id)
            if not latest_tags:
                continue
            latest_tags = normalize_tags(latest_tags)

            # Fetch latest state
            try:
                describe_resp = ec2_client.describe_instances(InstanceIds=[instance_id])
                reservations = describe_resp.get("Reservations", [])
                if not reservations or not reservations[0]["Instances"]:
                    logger.warning(f"No instance info found for {instance_id}")
                    continue
                latest_state = reservations[0]["Instances"][0]["State"]["Name"]
            except ClientError as e:
                logger.error(f"Failed to fetch instance state for {instance_id}: {e}")
                continue

            # If any difference found
            if stored_tags != latest_tags or stored_state != latest_state:
                table.update_item(
                    Key={'InstanceId': instance_id},
                    UpdateExpression="SET Tags = :tags, LastUpdated = :updated, #s = :state",
                    ExpressionAttributeNames={
                        '#s': 'State'
                    },
                    ExpressionAttributeValues={
                        ':tags': latest_tags,
                        ':updated': datetime.now(timezone.utc).isoformat(),
                        ':state': latest_state
                    }

                )
                logger.info(f"Refreshed DynamoDB for {instance_id} (tags/state changed)")
            else:
                logger.info(f"Record for {instance_id} is up-to-date")

    except ClientError as e:
        logger.error(f"DynamoDB scan or update failed: {e}")

def handle_sqs_event(record):
    try:
        body = json.loads(record['body'])
        message = json.loads(body['Message']) if 'Message' in body else body
        logger.info(f"Handling message: {json.dumps(message)}")

        detail_type = message.get('detail-type')

        if detail_type == 'EC2 Instance State-change Notification':
            handle_instance_state_change(message)
        elif detail_type in [
            'AWS API Call via CloudTrail',
            'EC2 Tag Change',
            'EC2 Instance Tag Change'
        ]:
            handle_tag_change_event(message)
        else:
            logger.info(f"Ignoring event of type: {detail_type}")

    except Exception as e:
        logger.error(f"Failed to process SQS record: {e}")
        raise

def lambda_handler(event, context):
    logger.info(f"Event: {json.dumps(event)}")

    if 'Records' in event:
        for record in event['Records']:
            handle_sqs_event(record)

    elif event.get('source') == 'aws.events':
        logger.info("Scheduled tagging/state checking event triggered")
        refresh_stale_tag_data()
