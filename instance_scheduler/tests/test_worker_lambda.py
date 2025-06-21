import json
import pytest
import os
from unittest.mock import patch, MagicMock


# Patch environment variables
os.environ['DDB_TABLE_NAME'] = 'TestTable'
os.environ['ORG_MEMBER_ROLE_NAME'] = 'EC2SchedulerRole'
os.environ['SQS_QUEUE_URL'] = 'https://sqs.eu-west-1.amazonaws.com/123456789012/InstanceSchedulerQueue'
os.environ['EVENT_BUS_NAME'] = 'InstanceSchedulerCentralBus'

import worker_lambda as scheduler

@pytest.fixture
def ec2_mock():
    mock_client = MagicMock()
    mock_client.describe_tags.return_value = {
        'Tags': [
            {'Key': 'Name', 'Value': 'test-instance'}
        ]
    }
    mock_client.describe_instances.return_value = {
        'Reservations': [{
            'Instances': [{
                'State': {'Name': 'running'}
            }]
        }]
    }
    yield mock_client

@patch('worker_lambda.sts.assume_role')
@patch('worker_lambda.boto3.client')
def test_assume_role_success(mock_boto_client, mock_assume_role):
    mock_assume_role.return_value = {
        'Credentials': {
            'AccessKeyId': 'test',
            'SecretAccessKey': 'test',
            'SessionToken': 'test'
        }
    }
    mock_boto_client.return_value = MagicMock()
    ec2_client = scheduler.assume_role("123456789012", "EC2SchedulerRole")
    assert ec2_client is not None

def test_normalize_tags():
    tags = {'Name': 'AppServer', 'AutoStop': '20:00'}
    norm = scheduler.normalize_tags(tags)
    assert norm == {'Name': 'AppServer', 'AutoStop': '20:00'}

@patch('worker_lambda.assume_role')
@patch('worker_lambda.fetch_instance_tags')
@patch('worker_lambda.table')
def test_handle_instance_state_change(mock_table, mock_fetch_tags, mock_assume_role, ec2_mock):
    mock_assume_role.return_value = ec2_mock
    mock_fetch_tags.return_value = {
        'Name': 'app-server'
    }

    message = {
        'detail': {
            'instance-id': 'i-abc123',
            'state': 'running'
        },
        'account': '123456789012'
    }

    scheduler.handle_instance_state_change(message)

    mock_table.put_item.assert_called()
    args = mock_table.put_item.call_args[1]
    assert args['Item']['InstanceId'] == 'i-abc123'
    assert 'Tags' in args['Item']
    assert args['Item']['State'] == 'running'

@patch('worker_lambda.assume_role')
@patch('worker_lambda.fetch_instance_tags')
@patch('worker_lambda.table')
def test_handle_tag_change_event(mock_table, mock_fetch_tags, mock_assume_role, ec2_mock):
    mock_assume_role.return_value = ec2_mock
    mock_fetch_tags.return_value = {'Env': 'dev'}

    message = {
        'detail': {
            'resource-id': 'i-abc456'
        },
        'account': '123456789012'
    }

    scheduler.handle_tag_change_event(message)
    mock_table.put_item.assert_called()
    args = mock_table.put_item.call_args[1]
    assert args['Item']['InstanceId'] == 'i-abc456'
    assert 'Env' in args['Item']['Tags']

@patch('worker_lambda.assume_role')
@patch('worker_lambda.table.scan')
@patch('worker_lambda.fetch_instance_tags')
def test_refresh_stale_tag_data(mock_fetch_tags, mock_scan, mock_assume_role, ec2_mock):
    mock_scan.return_value = {
        'Items': [{
            'InstanceId': 'i-xyz789',
            'AccountId': '123456789012',
            'Tags': {'Name': 'old'},
            'State': 'stopped'
        }]
    }
    mock_assume_role.return_value = ec2_mock
    mock_fetch_tags.return_value = {'Name': 'test-instance'}

    with patch.object(scheduler.table, 'update_item') as mock_update:
        scheduler.refresh_stale_tag_data()
        mock_update.assert_called()

@patch('worker_lambda.handle_instance_state_change')
@patch('worker_lambda.handle_tag_change_event')
def test_handle_sqs_event(mock_tag_handler, mock_state_handler):
    event = {
        'body': json.dumps({
            'detail-type': 'EC2 Instance State-change Notification',
            'detail': {
                'instance-id': 'i-abc123'
            },
            'account': '123456789012'
        })
    }
    scheduler.handle_sqs_event(event)
    mock_state_handler.assert_called_once()

def test_lambda_handler_scheduled(monkeypatch):
    monkeypatch.setenv('AWS_EXECUTION_ENV', 'AWS_Lambda_python3.9')

    with patch('worker_lambda.refresh_stale_tag_data') as mock_refresh:
        event = {'source': 'aws.events'}
        scheduler.lambda_handler(event, None)
        mock_refresh.assert_called_once()
