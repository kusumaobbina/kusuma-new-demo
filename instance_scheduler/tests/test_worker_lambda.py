import os
import json
import pytest
import boto3
from moto import mock_dynamodb, mock_sts, mock_sqs, mock_events
from unittest.mock import patch, MagicMock

# Set environment variables used by worker_lambda
os.environ['DDB_TABLE_NAME'] = 'InstanceSchedulerTable'
os.environ['ORG_MEMBER_ROLE_NAME'] = 'EC2SchedulerRole'
os.environ['SQS_QUEUE_URL'] = 'https://sqs.mock.amazonaws.com/123456789012/InstanceSchedulerQueue'
os.environ['EVENT_BUS_NAME'] = 'InstanceSchedulerCentralBus'

import worker_lambda  # import after env vars set


@pytest.fixture(scope='function')
def dynamodb_table():
    with mock_dynamodb():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        table = dynamodb.create_table(
            TableName=os.environ['DDB_TABLE_NAME'],
            KeySchema=[{'AttributeName': 'InstanceId', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'InstanceId', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        table.wait_until_exists()
        yield table


@pytest.fixture(autouse=True)
def mock_sts_sqs_events():
    with mock_sts(), mock_sqs(), mock_events():
        yield


def boto3_client_side_effect(service_name, *args, **kwargs):
    if service_name == 'dynamodb':
        # Return a client connected to moto's mocked dynamodb in us-east-1
        return boto3.client('dynamodb', region_name='us-east-1')
    elif service_name == 'ec2':
        mock_ec2_client = MagicMock()
        mock_ec2_client.describe_tags.return_value = {
            'Tags': [{'Key': 'Schedule', 'Value': 'start=08:00;stop=20:00'}]
        }
        mock_ec2_client.describe_instances.return_value = {
            'Reservations': [{
                'Instances': [{
                    'InstanceId': 'i-1234567890abcdef0',
                    'State': {'Name': 'stopped'}
                }]
            }]
        }
        mock_ec2_client.start_instances.return_value = {}
        mock_ec2_client.stop_instances.return_value = {}
        mock_ec2_client.create_tags.return_value = {}
        return mock_ec2_client
    elif service_name == 'sts':
        return boto3.client('sts', region_name='us-east-1')
    else:
        return boto3.client(service_name, *args, **kwargs)


@patch('worker_lambda.boto3.client', side_effect=boto3_client_side_effect)
def test_handle_sqs_event_and_put_dynamodb(mock_boto_client, dynamodb_table):
    # Sample SQS event with EC2 state change message
    event = {
        'Records': [{
            'body': json.dumps({
                'Message': json.dumps({
                    'detail-type': 'EC2 Instance State-change Notification',
                    'detail': {
                        'instance-id': 'i-1234567890abcdef0',
                        'account': '123456789012'
                    }
                })
            })
        }]
    }

    # Call the lambda handler
    worker_lambda.lambda_handler(event, None)

    # Verify the item was inserted into DynamoDB
    response = dynamodb_table.get_item(Key={'InstanceId': 'i-1234567890abcdef0'})
    assert 'Item' in response
    item = response['Item']
    assert item['InstanceId'] == 'i-1234567890abcdef0'
    assert item['AccountId'] == '123456789012'
    assert 'Tags' in item
    assert item['Tags']['Schedule'] == 'start=08:00;stop=20:00'


def test_lambda_handler_logs_missing_data(caplog):
    # Event missing instance-id/account to test warnings
    event = {
        'Records': [{
            'body': json.dumps({
                'Message': json.dumps({
                    'detail-type': 'EC2 Instance State-change Notification',
                    'detail': {}
                })
            })
        }]
    }
    with caplog.at_level('WARNING'):
        worker_lambda.lambda_handler(event, None)
        assert 'Missing instance-id or account-id' in caplog.text


@patch('worker_lambda.put_event_to_bus')
def test_scheduled_tagging_invocation(mock_put_event):
    # Test eventbridge scheduled event triggers tagging logic
    event = {'source': 'aws.events'}

    worker_lambda.lambda_handler(event, None)
    mock_put_event.assert_called()

