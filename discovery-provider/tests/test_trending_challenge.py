import logging
from datetime import date, datetime, timedelta
from src.trending_strategies.trending_type_and_version import TrendingType
from src.models.models import UserChallenge
from src.models import TrendingResult, AggregatePlays
from src.challenges.trending_challenge import should_trending_challenge_update
import redis

from src.models import User, Block
from src.utils.db_session import get_db
from src.challenges.trending_challenge import (
    trending_track_challenge_manager,
    trending_underground_track_challenge_manager,
    trending_playlist_challenge_manager,
)
from src.challenges.challenge_event_bus import ChallengeEventBus, ChallengeEvent
from src.utils.config import shared_config
from src.tasks.calculate_trending_challenges import enqueue_trending_challenges
from tests.utils import populate_mock_db

REDIS_URL = shared_config["redis"]["url"]
logger = logging.getLogger(__name__)


def test_trending_challenge_should_update(app):
    with app.app_context():
        db = get_db()
    redis_conn = redis.Redis.from_url(url=REDIS_URL)

    with db.scoped_session() as session:

        # ========== Test timestamp w/out trending result in DB ==========

        # If the timestamp is outside of threshold and nothing in db
        # Wrong time, wrong day
        timestamp = 1629132028
        should_update = should_trending_challenge_update(redis_conn, session, timestamp)
        assert not should_update

        # Right time, wrong day
        timestamp = 1629140400
        should_update = should_trending_challenge_update(redis_conn, session, timestamp)
        assert not should_update

        # wrong time, right day
        timestamp = 1629489600
        should_update = should_trending_challenge_update(redis_conn, session, timestamp)
        assert not should_update

        # Within bounds
        timestamp = 1629486000
        should_update = should_trending_challenge_update(redis_conn, session, timestamp)
        assert should_update

        # ========== Test working timestamp with trending result in DB ==========
        session.add(
            TrendingResult(
                user_id=1,
                rank=1,
                id="1",
                type="tracks",
                version="ePWJD",
                week="2021-08-20",
            )
        )
        session.flush()

        # Test same date as inserted trending result, so return false
        timestamp = 1629486000
        should_update = should_trending_challenge_update(redis_conn, session, timestamp)
        assert not should_update

        # Test week after inserted trending result, so return true
        timestamp = 1630090800
        should_update = should_trending_challenge_update(redis_conn, session, timestamp)
        assert should_update


def test_trending_challenge_job(app):
    with app.app_context():
        db = get_db()
    redis_conn = redis.Redis.from_url(url=REDIS_URL)

    test_entities = {
        "tracks": [
            {"track_id": 1, "owner_id": 1},
            {"track_id": 2, "owner_id": 2},
            {"track_id": 3, "owner_id": 3},
            {"track_id": 4, "owner_id": 4},
            {"track_id": 5, "owner_id": 5},
            {"track_id": 6, "owner_id": 2},
            {"track_id": 7, "owner_id": 3},
            {"track_id": 8, "owner_id": 3},
            {"track_id": 9, "is_unlisted": True, "owner_id": 3},
            {"track_id": 11, "owner_id": 1},
            {"track_id": 12, "owner_id": 2},
            {"track_id": 13, "owner_id": 3},
            {"track_id": 14, "owner_id": 4},
            {"track_id": 15, "owner_id": 5},
        ],
        "playlists": [
            {
                "playlist_id": 1,
                "playlist_owner_id": 1,
                "playlist_contents": {
                    "track_ids": [
                        {"track": 1},
                        {"track": 2},
                        {"track": 3},
                    ]
                },
            },
            {
                "playlist_id": 2,
                "playlist_owner_id": 2,
                "playlist_contents": {
                    "track_ids": [
                        {"track": 1},
                        {"track": 2},
                        {"track": 3},
                    ]
                },
            },
            {
                "playlist_id": 3,
                "is_album": True,
                "playlist_owner_id": 3,
                "playlist_contents": {
                    "track_ids": [
                        {"track": 1},
                        {"track": 2},
                        {"track": 3},
                    ]
                },
            },
            {
                "playlist_id": 4,
                "playlist_owner_id": 4,
                "playlist_contents": {
                    "track_ids": [
                        {"track": 1},
                        {"track": 2},
                        {"track": 3},
                    ]
                },
            },
            {
                "playlist_id": 5,
                "playlist_owner_id": 5,
                "playlist_contents": {
                    "track_ids": [
                        {"track": 1},
                        {"track": 2},
                        {"track": 3},
                    ]
                },
            },
        ],
        "users": [
            {"user_id": 1, "handle": "user1"},
            {"user_id": 2, "handle": "user2"},
            {"user_id": 3, "handle": "user3"},
            {"user_id": 4, "handle": "user4"},
            {"user_id": 5, "handle": "user5"},
        ],
        "follows": [
            {
                "follower_user_id": 1,
                "followee_user_id": 2,
                "created_at": datetime.now() - timedelta(days=8),
            },
            {
                "follower_user_id": 1,
                "followee_user_id": 3,
                "created_at": datetime.now() - timedelta(days=8),
            },
            {
                "follower_user_id": 2,
                "followee_user_id": 3,
                "created_at": datetime.now() - timedelta(days=8),
            },
            {
                "follower_user_id": 2,
                "followee_user_id": 4,
                "created_at": datetime.now() - timedelta(days=8),
            },
            {
                "follower_user_id": 3,
                "followee_user_id": 6,
                "created_at": datetime.now() - timedelta(days=8),
            },
            {
                "follower_user_id": 4,
                "followee_user_id": 5,
                "created_at": datetime.now() - timedelta(days=8),
            },
            {
                "follower_user_id": 5,
                "followee_user_id": 1,
                "created_at": datetime.now() - timedelta(days=8),
            },
            {
                "follower_user_id": 6,
                "followee_user_id": 3,
                "created_at": datetime.now() - timedelta(days=8),
            },
        ],
        "reposts": [
            {"repost_item_id": 1, "repost_type": "track", "user_id": 2},
            {"repost_item_id": 1, "repost_type": "playlist", "user_id": 2},
            {"repost_item_id": 3, "repost_type": "track", "user_id": 3},
            {"repost_item_id": 1, "repost_type": "playlist", "user_id": 3},
            {"repost_item_id": 4, "repost_type": "track", "user_id": 1},
            {"repost_item_id": 5, "repost_type": "track", "user_id": 1},
            {"repost_item_id": 6, "repost_type": "track", "user_id": 1},
        ],
        "saves": [
            {"save_item_id": 1, "save_type": "track", "user_id": 2},
            {"save_item_id": 1, "save_type": "track", "user_id": 3},
            {"save_item_id": 4, "save_type": "track", "user_id": 1},
            {"save_item_id": 5, "save_type": "track", "user_id": 1},
            {"save_item_id": 6, "save_type": "track", "user_id": 1},
            {"save_item_id": 1, "save_type": "playlist", "user_id": 4},
            {"save_item_id": 2, "save_type": "playlist", "user_id": 3},
            {"save_item_id": 3, "save_type": "playlist", "user_id": 2},
            {"save_item_id": 4, "save_type": "playlist", "user_id": 1},
            {"save_item_id": 5, "save_type": "playlist", "user_id": 2},
        ],
        "plays": [{"item_id": 1} for _ in range(55)]
        + [{"item_id": 2} for _ in range(60)]
        + [{"item_id": 3} for _ in range(70)]
        + [{"item_id": 4} for _ in range(90)]
        + [{"item_id": 5} for _ in range(80)]
        + [{"item_id": 6} for _ in range(40)]
        + [{"item_id": 11} for _ in range(200)]
        + [{"item_id": 12} for _ in range(200)]
        + [{"item_id": 13} for _ in range(200)]
        + [{"item_id": 14} for _ in range(200)]
        + [{"item_id": 15} for _ in range(200)],
    }

    populate_mock_db(db, test_entities)
    bus = ChallengeEventBus(redis_conn)

    # Register events with the bus
    bus.register_listener(
        ChallengeEvent.trending_underground,
        trending_underground_track_challenge_manager,
    )
    bus.register_listener(
        ChallengeEvent.trending_track, trending_track_challenge_manager
    )
    bus.register_listener(
        ChallengeEvent.trending_playlist, trending_playlist_challenge_manager
    )

    trending_date = date.fromisoformat("2021-08-20")

    with db.scoped_session() as session:
        session.execute("REFRESH MATERIALIZED VIEW aggregate_plays")
        session.execute("REFRESH MATERIALIZED VIEW aggregate_user")
        session.commit()

    enqueue_trending_challenges(db, redis_conn, bus, trending_date)

    with db.scoped_session() as session:
        bus.process_events(session)
        session.flush()
        trending_tracks = (
            session.query(TrendingResult)
            .filter(TrendingResult.type == str(TrendingType.TRACKS))
            .all()
        )
        assert len(trending_tracks) == 5

        trending_playlists = (
            session.query(TrendingResult)
            .filter(TrendingResult.type == str(TrendingType.PLAYLISTS))
            .all()
        )
        assert len(trending_playlists) == 5
