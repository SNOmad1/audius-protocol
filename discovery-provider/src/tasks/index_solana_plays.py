import binascii
import codecs
import logging

import base58
from src.models import Play
from src.tasks.celery_app import celery
from src.utils.config import shared_config
from src.utils.redis_cache import get_pickled_key, pickle_and_set

# TODO: These are configs
TRACK_LISTEN_PROGRAM = shared_config["solana"]["program_address"]
SECP_PROGRAM = "KeccakSecp256k11111111111111111111111111111"
SLEEP_TIME = 1

SOL_PLAYS_REDIS_KEY = "sol_plays"

logger = logging.getLogger(__name__)


# Actively connect to all peers in parallel
def process_solana_plays(solana_client):
    redis = index_solana_plays.redis
    db = index_solana_plays.db

    slot_from = get_pickled_key(redis, SOL_PLAYS_REDIS_KEY)
    if not slot_from:
        slot_from = solana_client.get_slot()["result"]
        pickle_and_set(redis, SOL_PLAYS_REDIS_KEY, slot_from)

    # slot_from = solana_client.get_slot()["result"]
    transaction = solana_client.get_confirmed_signature_for_address2(
        TRACK_LISTEN_PROGRAM, limit=1)
    if transaction["result"][0]["slot"] > slot_from:
        slot_from = transaction["result"][0]["slot"]
        pickle_and_set(redis, SOL_PLAYS_REDIS_KEY, slot_from)
        # TODO: What happens if there is >1 tx since the last iteration here?
        tx_info = solana_client.get_confirmed_transaction(
            transaction["result"][0]["signature"]
        )
        if SECP_PROGRAM in tx_info["result"]["transaction"]["message"]["accountKeys"]:
            audius_program_index = tx_info["result"]["transaction"]["message"]["accountKeys"].index(
                TRACK_LISTEN_PROGRAM
            )
            for instruction in tx_info["result"]["transaction"]["message"]["instructions"]:
                if instruction["programIdIndex"] == audius_program_index:
                    hex_data = binascii.hexlify(
                        bytearray(list(base58.b58decode(instruction["data"])))
                    )

                    l1 = int(hex_data[2:4], 16)
                    start_data1 = 10
                    end_data1 = l1 * 2 + start_data1

                    l2 = int(hex_data[end_data1:end_data1 + 2], 16)
                    start_data2 = end_data1 + 8
                    end_data2 = l2 * 2 + start_data2

                    l3 = int(hex_data[end_data2:end_data2 + 2], 16)
                    start_data3 = end_data2 + 8
                    end_data3 = l3 * 2 + start_data3

                    user_id = codecs.decode(hex_data[start_data1:end_data1], "hex")
                    track_id = codecs.decode(hex_data[start_data2:end_data2], "hex")
                    source = codecs.decode(hex_data[start_data3:end_data3], "hex")

                    logger.error(
                        f"index_solana_plays.py | user_id: {user_id} track_id: {track_id} source: {source}"
                    )
                    logger.error(
                        f"index_solana_plays.py | Got transaction: {tx_info}"
                    )
                    with db.scoped_session() as session:
                        session.add(
                            Play(
                                user_id=int(user_id),
                                play_item_id=int(track_id),
                                source=str(source, "utf-8"),
                            ))


######## CELERY TASKS ########
@celery.task(name="index_solana_plays", bind=True)
def index_solana_plays(self):
    # Cache custom task class properties
    # Details regarding custom task context can be found in wiki
    # Custom Task definition can be found in src/__init__.py
    redis = index_solana_plays.redis
    solana_client = index_solana_plays.solana_client
    # Define lock acquired boolean
    have_lock = False
    # Define redis lock object
    update_lock = redis.lock("solana_plays_lock", timeout=7200)
    try:
        # Attempt to acquire lock - do not block if unable to acquire
        have_lock = update_lock.acquire(blocking=False)
        if have_lock:
            process_solana_plays(solana_client)
        else:
            logger.info(
                "index_solana_plays.py | Failed to acquire index_solana_plays")
    except Exception as e:
        logger.error("index_solana_plays.py | Fatal error in main loop",
                     exc_info=True)
        raise e
    finally:
        if have_lock:
            update_lock.release()