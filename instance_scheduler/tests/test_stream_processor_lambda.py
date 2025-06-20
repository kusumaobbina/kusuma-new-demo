import os
import json
# Set environment variables if needed (optional)
os.environ['DDB_TABLE_NAME'] = 'InstanceSchedulerTable'
os.environ['ORG_MEMBER_ROLE_NAME'] = 'EC2SchedulerRole'
import pytest
from unittest.mock import patch, MagicMock
import stream_processor_lambda  # your lambda file



@pytest.fixture
def dynamodb_stream_event():
    return {
        "Records": [
            {
                "eventName": "INSERT",
                "dynamodb": {
                    "NewImage": {
                        "InstanceId": {"S": "i-1234567890abcdef0"},
                        "AccountId": {"S": "123456789012"},
                        "Tags": {"M": {"Schedule": {"S": "start=08:00;stop=20:00"}}}
                    }
                }
            }
        ]
    }

@patch('stream_processor_lambda.boto3.client')
@patch('stream_processor_lambda.assume_role')
@patch('stream_processor_lambda.should_start_stop')
def test_process_record_starts_instance(mock_should_start_stop, mock_assume_role, mock_boto_client, dynamodb_stream_event):
    # Mock schedule evaluation to say "should start"
    mock_should_start_stop.return_value = (True, False)  # start=True, stop=False

    # Mock assume_role to return a fake EC2 client
    mock_ec2 = MagicMock()
    mock_assume_role.return_value = mock_ec2

    # Mock describe_instances to say instance is stopped
    mock_ec2.describe_instances.return_value = {
        'Reservations': [{'Instances': [{'State': {'Name': 'stopped'}}]}]
    }

    # Call your process_record function with the first record
    record = dynamodb_stream_event['Records'][0]
    stream_processor_lambda.process_record(record)

    # Assert EC2 start_instances called for the stopped instance
    mock_ec2.start_instances.assert_called_once_with(InstanceIds=['i-1234567890abcdef0'])

@patch('stream_processor_lambda.boto3.client')
@patch('stream_processor_lambda.assume_role')
@patch('stream_processor_lambda.should_start_stop')
def test_process_record_stops_instance(mock_should_start_stop, mock_assume_role, mock_boto_client, dynamodb_stream_event):
    # Mock schedule evaluation to say "should stop"
    mock_should_start_stop.return_value = (False, True)  # start=False, stop=True

    # Mock assume_role to return a fake EC2 client
    mock_ec2 = MagicMock()
    mock_assume_role.return_value = mock_ec2

    # Mock describe_instances to say instance is running
    mock_ec2.describe_instances.return_value = {
        'Reservations': [{'Instances': [{'State': {'Name': 'running'}}]}]
    }

    # Call your process_record function with the first record
    record = dynamodb_stream_event['Records'][0]
    stream_processor_lambda.process_record(record)

    # Assert EC2 stop_instances called for the running instance
    mock_ec2.stop_instances.assert_called_once_with(InstanceIds=['i-1234567890abcdef0'])

@patch('stream_processor_lambda.process_record')
def test_lambda_handler_calls_process_record(mock_process_record, dynamodb_stream_event):
    # Call lambda_handler with the stream event
    stream_processor_lambda.lambda_handler(dynamodb_stream_event, None)

    # Assert process_record was called once per record
    assert mock_process_record.call_count == len(dynamodb_stream_event['Records'])

def test_lambda_handler_empty_records():
    event = {"Records": []}
    result = stream_processor_lambda.lambda_handler(event, None)
    assert result is None  # or whatever your lambda returns for empty events
