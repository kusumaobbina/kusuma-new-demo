import json
import os
import logging
import boto3
from datetime import datetime, timezone
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
table_name = os.environ['DDB_TABLE_NAME']
table = dynamodb.Table(table_name)

org_member_role_name = os.environ['ORG_MEMBER_ROLE_NAME']


def assume_role(account_id, role_name):
    sts = boto3.client('sts')
    try:
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        response = sts.assume_role(RoleArn=role_arn, RoleSessionName='EC2SchedulerSession')
        creds = response['Credentials']
        return boto3.client(
            'ec2',
            aws_access_key_id=creds['AccessKeyId'],
            aws_secret_access_key=creds['SecretAccessKey'],
            aws_session_token=creds['SessionToken'],
            region_name='eu-west-1'
        )
    except Exception as e:
        logger.error(f"Failed to assume role in account {account_id}: {e}")
        return None


def get_current_utc_time():
    return datetime.utcnow().strftime('%H:%M')

def parse_time_str(tstr):
    try:
        return datetime.strptime(tstr, '%H:%M').time()
    except Exception:
        return None

def take_instance_action(ec2_client, instance_id, tags, current_state, ddb_item):
    now_time = datetime.utcnow().time()
    timestamp_now = datetime.now(timezone.utc).isoformat()
    
    auto_start_str = tags.get('AutoStart')
    auto_stop_str = tags.get('AutoStop')
    last_updated = ddb_item.get('LastUpdated', '')  # or LastActionTime if you want
    
    def is_recent_action():
        if not last_updated:
            return False
        try:
            last_time = datetime.fromisoformat(last_updated)
            delta = datetime.now(timezone.utc) - last_time
            return delta.total_seconds() < 60  # avoid repeat within 60 seconds
        except Exception:
            return False
    
    auto_start = parse_time_str(auto_start_str)
    auto_stop = parse_time_str(auto_stop_str)

    if not auto_start or not auto_stop:
        logger.warning(f"[{instance_id}] Invalid AutoStart or AutoStop tags: {auto_start_str}, {auto_stop_str}")
        return
    
    try:
        # Define allowed running window
        if auto_start < auto_stop:
            # Normal case: office hours e.g. 08:00 to 20:00
            within_office_hours = auto_start <= now_time < auto_stop
        else:
            # Edge case: overnight e.g. 20:00 to 06:00
            within_office_hours = now_time >= auto_start or now_time < auto_stop

        if within_office_hours and current_state != 'running' and not is_recent_action():
            logger.info(f"[{instance_id}] Starting instance during office hours at {now_time}")
            ec2_client.start_instances(InstanceIds=[instance_id])
            table.update_item(
                Key={'InstanceId': instance_id},
                UpdateExpression="SET #s = :state, LastUpdated = :updated",
                ExpressionAttributeNames={"#s": "State"},
                ExpressionAttributeValues={
                    ":state": "pending",
                    ":updated": timestamp_now
                }
            )
        elif not within_office_hours and current_state == 'running' and not is_recent_action():
            logger.info(f"[{instance_id}] Stopping instance outside office hours at {now_time}")
            ec2_client.stop_instances(InstanceIds=[instance_id])
            table.update_item(
                Key={'InstanceId': instance_id},
                UpdateExpression="SET #s = :state, LastUpdated = :updated",
                ExpressionAttributeNames={"#s": "State"},
                ExpressionAttributeValues={
                    ":state": "stopping",
                    ":updated": timestamp_now
                }
            )
        else:
            logger.info(f"[{instance_id}] No action needed at {now_time}. State: {current_state}")
    except Exception as e:
        logger.error(f"Failed to take action on instance {instance_id}: {e}")


def parse_tags(tag_field):
    tags = {}
    if 'M' in tag_field:
        for k, v in tag_field['M'].items():
            tags[k] = v.get('S')
    elif 'L' in tag_field:
        for tag in tag_field['L']:
            key = tag['M']['Key']['S']
            val = tag['M']['Value']['S']
            tags[key] = val
    return tags


def process_stream_record(record):
    event_name = record.get('eventName')
    ddb = record['dynamodb']
    
    if event_name == 'REMOVE':
        old_image = ddb.get('OldImage', {})
        instance_id = old_image.get('InstanceId', {}).get('S')
        if instance_id:
            try:
                table.delete_item(Key={'InstanceId': instance_id})
                logger.info(f"Deleted terminated instance {instance_id} from DynamoDB")
            except Exception as e:
                logger.error(f"Error deleting {instance_id} from DynamoDB: {e}")
        return

    if event_name not in ['INSERT', 'MODIFY']:
        return

    new_image = ddb.get('NewImage', {})
    instance_id = new_image.get('InstanceId', {}).get('S')
    account_id = new_image.get('AccountId', {}).get('S')
    state = new_image.get('State', {}).get('S')
    tags = parse_tags(new_image.get('Tags', {}))

    if not instance_id or not account_id:
        logger.warning("Missing instance-id or account-id in DynamoDB stream record")
        return

    if state == 'terminated':
        try:
            table.delete_item(Key={'InstanceId': instance_id})
            logger.info(f"Deleted terminated instance {instance_id} from DynamoDB")
        except Exception as e:
            logger.error(f"Error deleting {instance_id} from DynamoDB: {e}")
        return

    if 'platform' not in tags:
        logger.info(f"Instance {instance_id} does not have 'platform' tag. Skipping.")
        return

    ec2_client = assume_role(account_id, org_member_role_name)
    if ec2_client:
        take_instance_action(ec2_client, instance_id, tags, state, new_image)


def process_all_instances():
    try:
        response = table.scan()
        for item in response.get('Items', []):
            instance_id = item.get('InstanceId')
            account_id = item.get('AccountId')
            state = item.get('State')
            tags = item.get('Tags', {})

            if not instance_id or not account_id or not isinstance(tags, dict):
                continue

            if 'platform' not in tags:
                logger.info(f"Skipping {instance_id} (missing 'platform')")
                continue

            ec2_client = assume_role(account_id, org_member_role_name)
            if ec2_client:
                take_instance_action(ec2_client, instance_id, tags, state, item)
    except Exception as e:
        logger.error(f"Error processing all instances during scheduled run: {e}")


def lambda_handler(event, context):
    logger.info(f"Received event: {json.dumps(event)}")

    # EventBridge Scheduled Trigger
    if event.get("source") == "aws.events" and event.get("detail-type") == "Scheduled Event":
        logger.info("Processing EventBridge scheduled check.")
        process_all_instances()
        return

    # DynamoDB Stream Events
    for record in event.get('Records', []):
        process_stream_record(record)
