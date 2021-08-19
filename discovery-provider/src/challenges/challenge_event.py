import enum

# Needs to be in it's own file, otherwise
# we get circular imports


class ChallengeEvent(str, enum.Enum):
    profile_update = "profile_update"
    repost = "repost"
    follow = "follow"
    favorite = "favorite"
    track_listen = "track_listen"
    track_upload = "track_upload"
    connect_verified = "connect_verified"
    trending_track = "trending_track"
    trending_underground = "trending_underground"
    trending_playlist = "trending_playlist"
