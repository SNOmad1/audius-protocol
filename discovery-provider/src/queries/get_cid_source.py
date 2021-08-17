import logging
import sqlalchemy

from src.utils import db_session
 
logger = logging.getLogger(__name__)


def get_cid_source(cid):
    """
    Returns the CID source (e.g. CID is a metadata hash, a cover photo, a track segment, etc.)

    Args: the observed CID
    """
    if cid is None:
        raise ArgumentError("Input CID is invalid")

    db = db_session.get_db_read_replica()
    with db.scoped_session() as session:
        # Check to see if CID is of any type but a segment
        cid_source_res = sqlalchemy.text(
            """
            WITH cid_const AS (VALUES (:cid)) 
            SELECT * FROM 
            (
                (
                    SELECT 
                        "user_id" as "id", 
                        'users' as "table_name",
                        'metadata_multihash' as "type",
                        "is_current"
                    FROM "users" WHERE  (table cid_const) = "metadata_multihash"
                )
                    UNION ALL
                    (
                        SELECT 
                            "user_id" as "id", 
                            'users' as "table_name",
                            'profile_cover_images' as "type",
                            "is_current"
                        FROM 
                            "users" 
                        WHERE 
                            (table cid_const) in (
                            "profile_picture", 
                            "cover_photo", "profile_picture_sizes", 
                            "cover_photo_sizes"
                            )
                    ) 
                    UNION ALL 
                    (
                            SELECT 
                            "playlist_id" as "id", 
                            'playlists' as "table_name",
                            'playlist_image_multihash' as "type",
                            "is_current"
                            FROM 
                                "playlists" 
                            WHERE 
                                (table cid_const) in (
                                    "playlist_image_sizes_multihash", 
                                    "playlist_image_multihash"
                                )
                    ) 
                    UNION ALL 
                    (
                        SELECT 
                            "track_id" as "id", 
                            'tracks' as "table_name",
                            'track_metadata_or_cover_art_size' as "type",
                            "is_current"
                        FROM 
                            "tracks" 
                        WHERE 
                            (table cid_const) in (
                                "metadata_multihash",
                                "cover_art_sizes"
                            )
                    )
            ) as "outer"
            """
        )
        cid_source = session.execute(
            cid_source_res, {"cid": cid}
        ).fetchall()

        # If something is found, return it
        if len(cid_source) != 0:
            logger.warning(f"Found something: {[dict(row) for row in cid_source]}")
            return [dict(row) for row in cid_source]

        # Check to see if CID is a segment
        cid_source_res = sqlalchemy.text(
            """
            WITH cid_const AS (VALUES (:cid))
                SELECT 
                    "track_id" as "id", 
                    'tracks' as "table_name",
                    'segment' as "type"
                FROM 
                    (
                        SELECT 
                            jb -> 'duration' as "d", 
                            jb -> 'multihash' :: varchar as "cid", 
                            "track_id" 
                        FROM 
                            (
                                SELECT 
                                    jsonb_array_elements("track_segments") as "jb", 
                                    "track_id" 
                                FROM 
                                    "tracks"
                            ) as a
                    ) as a2 
                WHERE 
                    "cid" ? (table cid_const)
            """
        )

        cid_source = session.execute(
            cid_source_res, {"cid": cid}
        ).fetchall()

        # If something is found, return it
        if len(cid_source) != 0:
            logger.warning(f"Found something 2: {cid_source}")
            return [dict(row) for row in cid_source]

        # Nothing was found. CID is not present anywhere
        return [] 
        # if cid_source is None:
            # do the track segment query

        # return ^ response even if is None

        # users_dict = [dict(row) for row in users]