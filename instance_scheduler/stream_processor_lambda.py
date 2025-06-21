import json
import os
import logging
import boto3
from datetime import datetime, timezone, timedelta

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
table_name = os.environ['DDB_TABLE_NAME']
table = dynamodb.Table(table_name)
org_member_role_name = os.environ['ORG_MEMBER_ROLE_NAME']
sqs = boto3.client('sqs')
dlq_url = os.environ.get('STREAM_PROCESSOR_DLQ_URL')

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


def time_in_range(target_time, now_time, window_minutes=5, overnight_end=None):
    """
    Returns True if now_time is within ±window_minutes of target_time.
    Handles overnight windows if overnight_end time is provided.
    """
    delta = timedelta(minutes=window_minutes)
    target_dt = datetime.combine(datetime.today(), target_time)
    now_dt = datetime.combine(datetime.today(), now_time)
    
    if overnight_end and target_time > overnight_end:
        # Overnight window, check if now_time is after target_time or before overnight_end
        if now_time >= target_time or now_time <= overnight_end:
            # Adjust now_dt if before overnight_end (next day)
            if now_time <= overnight_end:
                now_dt += timedelta(days=1)
            diff = abs((now_dt - target_dt).total_seconds())
            return diff <= delta.total_seconds()
        return False
    else:
        diff = abs((now_dt - target_dt).total_seconds())
        return diff <= delta.total_seconds()


def take_instance_action(ec2_client, instance_id, tags, current_state, ddb_item):
    now_time = datetime.utcnow().time()
    timestamp_now = datetime.now(timezone.utc).isoformat()

    auto_start_str = tags.get('AutoStart')
    auto_stop_str = tags.get('AutoStop')
    last_updated = ddb_item.get('LastUpdated', '')

    def is_recent_action():
        if not last_updated:
            return False
        try:
            last_time = datetime.fromisoformat(last_updated)
            delta = datetime.now(timezone.utc) - last_time
            return delta.total_seconds() < 300  # 5 minutes window to prevent repeats
        except Exception:
            return False

    auto_start = parse_time_str(auto_start_str)
    auto_stop = parse_time_str(auto_stop_str)

    if not auto_start or not auto_stop:
        logger.warning(f"[{instance_id}] Invalid AutoStart or AutoStop tags: {auto_start_str}, {auto_stop_str}")
        return

    try:
        # Start instance only within ±5 mins of AutoStart time
        if time_in_range(auto_start, now_time):
            if current_state != 'running' and not is_recent_action():
                logger.info(f"[{instance_id}] Starting instance at AutoStart time {now_time}")
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
            else:
                logger.info(f"[{instance_id}] No start action needed at AutoStart time. State: {current_state}")

        else:
            # Determine if current time is outside office hours
            if auto_start < auto_stop:
                # Normal office hours window (e.g., 06:00 - 20:00)
                outside_office_hours = now_time >= auto_stop or now_time < auto_start
            else:
                # Overnight office hours window (e.g., 20:00 - 06:00)
                outside_office_hours = auto_stop <= now_time < auto_start

            if outside_office_hours:
                if current_state == 'running' and not is_recent_action():
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
                    logger.info(f"[{instance_id}] No stop action needed outside office hours. State: {current_state}")
            else:
                logger.info(f"[{instance_id}] Within office hours, no stop action needed. State: {current_state}")

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

def send_to_dlq(failed_event, error_message):
    if not dlq_url:
        logger.error("DLQ URL not configured, cannot send failed event")
        return

    message_body = {
        'failed_event': failed_event,
        'error': error_message,
        'timestamp': datetime.now(timezone.utc).isoformat()
    }

    try:
        sqs.send_message(
            QueueUrl=dlq_url,
            MessageBody=json.dumps(message_body)
        )
        logger.info("Sent failed event to DLQ")
    except Exception as e:
        logger.error(f"Failed to send event to DLQ: {e}")


def lambda_handler(event, context):
    logger.info(f"Received event: {json.dumps(event)}")

    # EventBridge Scheduled Trigger
    if event.get("source") == "aws.events" and event.get("detail-type") == "Scheduled Event":
        logger.info("Processing EventBridge scheduled check.")
        try:
            process_all_instances()
        except Exception as e:
            logger.error(f"Error in process_all_instances: {e}")
            send_to_dlq(event, str(e))
        return

    # DynamoDB Stream Events
    for record in event.get('Records', []):
        try:
            process_stream_record(record)
        except Exception as e:
            logger.error(f"Error processing record {record}: {e}")
            send_to_dlq(record, str(e))

