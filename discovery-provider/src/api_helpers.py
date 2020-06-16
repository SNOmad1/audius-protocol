import logging
import json
import datetime

from web3 import Web3
from web3.auto import w3
from eth_account.messages import encode_defunct
from hexbytes import HexBytes
from copy import deepcopy

from flask import jsonify
import redis
from src.utils.config import shared_config
from src.utils.redis_constants import latest_block_redis_key, most_recent_indexed_block_redis_key

import time
redis_url = shared_config["redis"]["url"]
redis = redis.Redis.from_url(url=redis_url)

logger = logging.getLogger(__name__)
METADATA_FIELDS = ['success', 'latest_indexed_block', 'latest_chain_block']
API_SIGNING_FIELDS = ['timestamp', 'signature']

# Subclass JSONEncoder
class DateTimeEncoder(json.JSONEncoder):
    # Override the default method
    def default(self, obj):
        if isinstance(obj, (datetime.date, datetime.datetime)):
            # TODO - the Z is required in JS date format
            return obj.isoformat() + " Z"
        
        return super().default(obj)

def error_response(error, error_code=500):
    return jsonify({'success': False, 'error': error}), error_code

def success_response(response_entity=None, status=200):
    response_dictionary = response_dict_with_metadata(response_entity)
    return jsonify(response_dictionary), status

# Create a response dict with just data, signature, and timestamp
def response_with_signature(response_entity=None, status=200):
    # Make copy of response entity
    response_dictionary = {**response_entity}
    generate_and_set_sig_and_timestamp(response_dictionary, response_entity)
    return jsonify(response_dictionary), status

# Create a response dict with metadata, data, signature, and timestamp
def success_response_with_signature(response_entity=None, status=200):
    # structure data to sign
    data = { 'data': response_entity }
    response_dictionary = response_dict_with_metadata(response_entity)    
    generate_and_set_sig_and_timestamp(response_dictionary, data)
    return jsonify(response_dictionary), status

# Create a response dict with metadata fields of success, latest_indexed_block, and latest_chain_block
def response_dict_with_metadata(response_entity=None):
    response_dictionary = {
        'data': response_entity
    }

    response_dictionary['success'] = True

    latest_indexed_block = redis.get(most_recent_indexed_block_redis_key)
    latest_chain_block = redis.get(latest_block_redis_key)

    response_dictionary['latest_indexed_block'] = (int(latest_indexed_block) if latest_indexed_block else None)
    response_dictionary['latest_chain_block'] = (int(latest_chain_block) if latest_chain_block else None)

    return response_dictionary

# Generate signature and timestamp using data and adding those fields to response dict
def generate_and_set_sig_and_timestamp(response_dict, data):
    signature, timestamp = generate_signature_and_timestamp(data)
    response_dict['signature'] = signature
    response_dict['timestamp'] = timestamp

# Generate signature and timestamp using data
def generate_signature_and_timestamp(data):
    # generate timestamp
    timestamp = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f Z')

    # combine timestamp and data to sign
    to_sign = {"timestamp": timestamp, **data}
    to_sign_str = json.dumps(to_sign, sort_keys=True, ensure_ascii=False, separators=(',', ':'), cls=DateTimeEncoder)

    # generate hash for if data contains unicode chars
    to_sign_hash = Web3.keccak(text=to_sign_str).hex()

    # generate SignableMessage for sign_message()
    encoded_to_sign = encode_defunct(hexstr=to_sign_hash)

    # sign to get signature
    signed_message = w3.eth.account.sign_message(encoded_to_sign, private_key=shared_config['delegate']['private_key'])
    return signed_message.signature.hex(), timestamp

# Accepts raw data with timestamp key and relevant fields, converts data to hash, and recovers the wallet
def recover_wallet(data, signature):
    json_dump = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'), cls=DateTimeEncoder)

    # generate hash for if data contains unicode chars
    to_recover_hash = Web3.keccak(text=json_dump).hex()

    encoded_to_recover = encode_defunct(hexstr=to_recover_hash)
    recovered_wallet = w3.eth.account.recover_message(encoded_to_recover, signature=signature)

    return recovered_wallet 
