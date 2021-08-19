import logging
from typing import Counter, Dict, Tuple, TypedDict, List, Optional, cast
from sqlalchemy.orm.session import Session
from sqlalchemy import func
from src.models.models import ChallengeType
from abc import ABC, abstractmethod
from src.models import Challenge, UserChallenge

logger = logging.getLogger(__name__)

# DB Accessors
def fetch_user_challenges(
    session: Session, challenge_id: str, specifiers: List[str]
) -> List[UserChallenge]:
    user_challenges = (
        session.query(UserChallenge).filter(
            UserChallenge.challenge_id == challenge_id,
            UserChallenge.specifier.in_(specifiers),
        )
    ).all()
    # Re-sort them
    specifier_map = {
        user_challenge.specifier: user_challenge for user_challenge in user_challenges
    }
    return [specifier_map[s] for s in specifiers if s in specifier_map]


class EventMetadata(TypedDict):
    block_number: int
    user_id: int
    extra: Dict


class FullEventMetadata(TypedDict):
    block_number: int
    user_id: int
    specifier: str
    extra: Dict


class ChallengeUpdater(ABC):
    """`ChallengeUpdater` is an abstract class which provides challenge specific logic
    to an instance of a `ChallengeManager`. The only required override is update_user_challenges
    """

    def update_user_challenges(
        self,
        session: Session,
        event: str,
        user_challenges: List[UserChallenge],
        step_count: Optional[int],
        event_metadatas: List[FullEventMetadata],
        starting_block: Optional[int],
    ):
        """This is usually the main required method to fill out when implementing a new challenge.
        Given an event type, a list of existing user challenges, and the base challenge type,
        update the given user_challenges.

        In the case of aggregate challenges, where UserChallenges are created in an
        already completed state, this method can be left as is.
        """

    def on_after_challenge_creation(self, session, metadatas: List[FullEventMetadata]):
        """Optional method to do some work after the `ChallengeManager` creates new challenges.
        If a challenge is backed by it's own table, for instance, create those rows here.
        """

    def generate_specifier(self, user_id: int, extra: Dict) -> str:
        """Optional method to provide a custom specifier for a challenge, given a user_id"""
        return str(user_id)

    def should_create_new_challenge(
        self, event: str, user_id: int, extra: Dict
    ) -> bool:
        """Optional method called for aggregate challenges to allow for overriding default
        behavior of creating a new UserChallenge whenever 1) we see a relevant event and
        2) the parent challenge is not yet complete.
        """
        return True

    def get_metadata(self, session: Session, specifiers: List[str]) -> List[Dict]:
        """Optional method to provide any extra metadata required for client to properly display a challenge."""
        return [{} for s in specifiers]

    def get_default_metadata(self) -> Dict:
        """Optional method to provide default metadata for an challenge with no progress."""
        return {}


class ChallengeManager:
    """`ChallengeManager` is responsible for handling shared logic between updating different challenges.
    To specialize a ChallengeManager for a particular challenge type, initialize it with a subclass
    of `ChallengeUpdater` implementing the business logic of that challenge.
    """

    challenge_id: str
    _did_init: bool
    _updater: ChallengeUpdater
    _starting_block: Optional[int]
    _step_count: Optional[int]
    _challenge_type: ChallengeType

    def __init__(self, challenge_id: str, updater: ChallengeUpdater):
        self.challenge_id = challenge_id
        self._did_init = False
        self._updater = updater
        self._starting_block = None
        self._step_count = None
        self._challenge_type = None  # type: ignore

    def process(self, session, event_type: str, event_metadatas: List[EventMetadata]):
        """Processes a number of events for a particular event type, updating
        UserChallengeEvents as needed.

        """
        if not self._did_init:  # lazy init
            self._init_challenge(session)

        # filter out events that took place before the starting block, returning
        # early if need be
        if self._starting_block is not None:
            event_metadatas = list(
                filter(
                    lambda x: x["block_number"] >= cast(int, self._starting_block),
                    event_metadatas,
                )
            )

        if not event_metadatas:
            return

        # Add specifiers
        events_with_specifiers: List[FullEventMetadata] = [
            {
                "user_id": event["user_id"],
                "block_number": event["block_number"],
                "extra": event["extra"],
                "specifier": self._updater.generate_specifier(
                    event["user_id"], event["extra"]
                ),
            }
            for event in event_metadatas
        ]

        # Drop any duplicate specifiers
        events_with_specifiers_map = {
            event["specifier"]: event for event in events_with_specifiers
        }
        events_with_specifiers = list(events_with_specifiers_map.values())

        specifiers: List[str] = [e["specifier"] for e in events_with_specifiers]

        # Gets all user challenges,
        existing_user_challenges = fetch_user_challenges(
            session, self.challenge_id, specifiers
        )

        # Create users that need challenges still
        existing_specifiers = {
            challenge.specifier for challenge in existing_user_challenges
        }

        # Create new challenges

        new_challenge_metadata = [
            metadata
            for metadata in events_with_specifiers
            if metadata["specifier"] not in existing_specifiers
        ]
        to_create_metadata: List[FullEventMetadata] = []
        if self._challenge_type == ChallengeType.aggregate:
            # For aggregate challenges, only create them
            # if we haven't maxed out completion yet, and
            # we haven't overriden this via should_create_new_challenge

            # Get *all* UserChallenges per user
            user_ids = list({e["user_id"] for e in event_metadatas})
            all_user_challenges: List[Tuple[int, int]] = (
                session.query(
                    UserChallenge.user_id, func.count(UserChallenge.specifier)
                )
                .filter(
                    UserChallenge.challenge_id == self.challenge_id,
                    UserChallenge.user_id.in_(user_ids),
                )
                .group_by(UserChallenge.user_id)
            ).all()
            challenges_per_user = dict(all_user_challenges)
            for new_metadata in new_challenge_metadata:
                completion_count = challenges_per_user.get(new_metadata["user_id"], 0)
                if self._step_count and completion_count >= self._step_count:
                    continue
                if not self._updater.should_create_new_challenge(
                    event_type, new_metadata["user_id"], new_metadata["extra"]
                ):
                    continue
                to_create_metadata.append(new_metadata)
        else:
            to_create_metadata = new_challenge_metadata

        new_user_challenges = [
            self._create_new_user_challenge(metadata["user_id"], metadata["specifier"])
            for metadata in to_create_metadata
        ]
        # Do any other custom work needed after creating a challenge event
        self._updater.on_after_challenge_creation(session, to_create_metadata)

        # Update all the challenges
        in_progress_challenges = [
            challenge
            for challenge in existing_user_challenges
            if not challenge.is_complete
        ]
        to_update = in_progress_challenges + new_user_challenges

        self._updater.update_user_challenges(
            session,
            event_type,
            to_update,
            self._step_count,
            events_with_specifiers,
            self._starting_block,
        )

        # Add block # to newly completed challenges
        for challenge in to_update:
            if challenge.is_complete:
                block_number = events_with_specifiers_map[challenge.specifier][
                    "block_number"
                ]
                challenge.completed_blocknumber = block_number

        logger.debug(f"Updated challenges from event [{event_type}]: [{to_update}]")
        # Only add the new ones
        session.add_all(new_user_challenges)

    def get_user_challenge_state(
        self, session: Session, specifiers: List[str]
    ) -> List[UserChallenge]:
        return fetch_user_challenges(session, self.challenge_id, specifiers)

    def get_metadata(self, session: Session, specifiers: List[str]) -> List[Dict]:
        """Gets additional metadata to render the challenge if needed."""
        return self._updater.get_metadata(session, specifiers)

    def get_default_metadata(self):
        """Gets default metadata for an challenge with no progress."""
        return self._updater.get_default_metadata()

    # Helpers

    def _init_challenge(self, session):
        challenge: Challenge = (
            session.query(Challenge).filter(Challenge.id == self.challenge_id).first()
        )
        if not challenge:
            raise Exception("No matching challenge!")
        self._starting_block = challenge.starting_block
        self._step_count = challenge.step_count
        self._challenge_type = challenge.type
        self._did_init = True

    def _create_new_user_challenge(self, user_id: int, specifier: str):
        return UserChallenge(
            challenge_id=self.challenge_id,
            user_id=user_id,
            specifier=specifier,
            is_complete=(
                self._challenge_type == ChallengeType.aggregate
            ),  # Aggregates are made in completed state
            current_step_count=0,
        )
