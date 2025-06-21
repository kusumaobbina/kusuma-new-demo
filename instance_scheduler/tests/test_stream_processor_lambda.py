import os
from unittest.mock import patch, MagicMock
# from datetime import datetime, timedelta, timezone
from freezegun import freeze_time
# Patch environment variables
os.environ['DDB_TABLE_NAME'] = 'TestTable'
os.environ['ORG_MEMBER_ROLE_NAME'] = 'EC2SchedulerRole'
os.environ['SQS_QUEUE_URL'] = 'https://sqs.eu-west-1.amazonaws.com/123456789012/InstanceSchedulerQueue'
os.environ['EVENT_BUS_NAME'] = 'InstanceSchedulerCentralBus'

import stream_processor_lambda as processor


def get_mock_ddb(last_updated_minutes_ago=10):
    # last_updated_time = (datetime.now(timezone.utc) - timedelta(minutes=last_updated_minutes_ago)).isoformat()
    return {
        'InstanceId': 'i-running-after-hours',
        'AccountId': '123456789012',
        'State': 'running',
        'Tags': {'AutoStart': '06:00', 'AutoStop': '20:00', 'platform': 'dev'},
        'LastUpdated': '20:00'
    }

# Existing tests here...


@freeze_time("2025-06-21 6:01:00", tz_offset=0)
@patch('stream_processor_lambda.table')
def test_start_instance_overnight_window(mock_table):
    mock_ec2 = MagicMock()
    # Overnight schedule: start 20:00, stop 06:00
    tags = {'AutoStart': '06:00', 'AutoStop': '20:00'}

    # Instance stopped at 21:00 (inside overnight "office hours")
    processor.take_instance_action(
        ec2_client=mock_ec2,
        instance_id="i-overnight-start",
        tags=tags,
        current_state="stopped",
        ddb_item=get_mock_ddb(10)
    )
    mock_ec2.start_instances.assert_called_once_with(InstanceIds=['i-overnight-start'])
    mock_ec2.stop_instances.assert_not_called()




@freeze_time("2025-06-21 20:01:00", tz_offset=0)
@patch('stream_processor_lambda.table')
def test_stop_instance_overnight_window(mock_table):
    mock_ec2 = MagicMock()
    tags = {'AutoStart': '06:00', 'AutoStop': '20:00'}


    processor.take_instance_action(
        ec2_client=mock_ec2,
        instance_id="i-overnight-stop",
        tags=tags,
        current_state="running",
        ddb_item=get_mock_ddb(10)  # Last updated 10 minutes ago
    )

    mock_ec2.stop_instances.assert_called_once_with(InstanceIds=['i-overnight-stop'])
    mock_ec2.start_instances.assert_not_called()


@freeze_time("2025-06-21 12:00:00", tz_offset=0)
@patch('stream_processor_lambda.table')
def test_invalid_tags_no_action(mock_table):
    mock_ec2 = MagicMock()
    tags = {'AutoStart': 'invalid', 'AutoStop': None}

    processor.take_instance_action(
        ec2_client=mock_ec2,
        instance_id="i-invalid-tags",
        tags=tags,
        current_state="stopped",
        ddb_item=get_mock_ddb(10)
    )
    mock_ec2.start_instances.assert_not_called()
    mock_ec2.stop_instances.assert_not_called()


@patch('stream_processor_lambda.table')
def test_process_stream_record_terminates_instance(mock_table):
    mock_table.delete_item = MagicMock()
    # DynamoDB REMOVE event with terminated instance
    record = {
        'eventName': 'REMOVE',
        'dynamodb': {
            'OldImage': {
                'InstanceId': {'S': 'i-terminated'}
            }
        }
    }
    processor.process_stream_record(record)
    mock_table.delete_item.assert_called_once_with(Key={'InstanceId': 'i-terminated'})


@patch('stream_processor_lambda.assume_role')
@patch('stream_processor_lambda.take_instance_action')
def test_process_stream_record_skips_missing_platform(mock_take_action, mock_assume_role):
    mock_take_action.reset_mock()
    mock_assume_role.return_value = MagicMock()
    # DynamoDB INSERT event without 'platform' tag
    record = {
        'eventName': 'INSERT',
        'dynamodb': {
            'NewImage': {
                'InstanceId': {'S': 'i-123'},
                'AccountId': {'S': '123456789012'},
                'State': {'S': 'running'},
                'Tags': {'M': {'SomeTag': {'S': 'Value'}}}  # no platform tag
            }
        }
    }
    processor.process_stream_record(record)
    mock_take_action.assert_not_called()

@freeze_time("2025-06-21 21:00:00", tz_offset=0)  # 9 PM UTC, after office hours
@patch('stream_processor_lambda.table')
def test_stop_instance_after_office_hours_on_scheduler_recheck(mock_table):
    mock_ec2 = MagicMock()

    ddb_item = get_mock_ddb(last_updated_minutes_ago=10)  # last updated 10 mins ago (not recent)

    # Call take_instance_action simulating the scheduler re-check
    processor.take_instance_action(
        ec2_client=mock_ec2,
        instance_id=ddb_item['InstanceId'],
        tags=ddb_item['Tags'],
        current_state=ddb_item['State'],
        ddb_item=ddb_item
    )

    # Assert stop_instances was called once, because instance is running after stop time
    mock_ec2.stop_instances.assert_called_once_with(InstanceIds=[ddb_item['InstanceId']])
    mock_ec2.start_instances.assert_not_called()
