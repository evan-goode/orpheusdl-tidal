import base64
import json
import logging
from getpass import getpass

from utils.models import *
from utils.utils import sanitise_name
from .tidal_api import TidalTvSession, TidalApi, TidalAuthError, SessionStorage, TidalMobileSession, SessionType

module_information = ModuleInformation(
    service_name='Tidal',
    module_supported_modes=ModuleModes.download | ModuleModes.credits | ModuleModes.lyrics,
    login_behaviour=ManualEnum.manual,
    global_settings={
        'tv_token': 'aR7gUaTK1ihpXOEP',
        'tv_secret': 'eVWBEkuL2FCjxgjOkR3yK0RYZEbcrMXRc2l8fU3ZCdE=',
        'mobile_token': 'dN2N95wCyEBTllu4',
        'enable_mobile': True
    },
    session_storage_variables=[SessionType.TV.name, SessionType.MOBILE.name],
    netlocation_constant='tidal',
    test_url='https://tidal.com/browse/track/92265335'
)


class ModuleInterface:
    def __init__(self, module_controller: ModuleController):
        self.cover_size = module_controller.orpheus_options.default_cover_options.resolution
        settings = module_controller.module_settings

        # LOW = 96kbit/s AAC, HIGH = 320kbit/s AAC, LOSSLESS = 44.1/16 FLAC, HI_RES <= 48/24 FLAC with MQA
        self.quality_parse = {
            QualityEnum.LOW: 'LOW',
            QualityEnum.MEDIUM: 'HIGH',
            QualityEnum.HIGH: 'HIGH',
            QualityEnum.LOSSLESS: 'LOSSLESS',
            QualityEnum.HIFI: 'HI_RES'
        }

        sessions = {}
        self.available_sessions = [SessionType.TV.name, SessionType.MOBILE.name]

        if settings['enable_mobile']:
            storage: SessionStorage = module_controller.temporary_settings_controller.read(SessionType.MOBILE.name)
            if not storage:
                confirm = input('"enable_mobile" is enabled but no MOBILE session was found. Do you want to create a '
                                'MOBILE session (used for AC-4/360RA) [Y/n]? ')
                if confirm.upper() == 'N':
                    self.available_sessions = [SessionType.TV.name]
        else:
            self.available_sessions = [SessionType.TV.name]

        for session_type in self.available_sessions:
            storage: SessionStorage = module_controller.temporary_settings_controller.read(session_type)

            if session_type == SessionType.TV.name:
                sessions[session_type] = TidalTvSession(settings['tv_token'], settings['tv_secret'])
            else:
                sessions[session_type] = TidalMobileSession(settings['mobile_token'])

            if storage:
                logging.debug(f'Tidal: {session_type} session found, loading')

                sessions[session_type].set_storage(storage)
            else:
                logging.debug(f'Tidal: No {session_type} session found, creating new one')
                if session_type == SessionType.TV.name:
                    sessions[session_type].auth()
                else:
                    print('Tidal: Enter your Tidal username and password:')
                    username = input('Username: ')
                    password = getpass('Password: ')
                    sessions[session_type].auth(username, password)
                    print('Successfully logged in!')

                module_controller.temporary_settings_controller.set(session_type, sessions[session_type].get_storage())

            # Always try to refresh session
            if not sessions[session_type].valid():
                sessions[session_type].refresh()
                # Save the refreshed session in the temporary settings
                module_controller.temporary_settings_controller.set(session_type, sessions[session_type].get_storage())

            while True:
                # Check for HiFi subscription
                try:
                    sessions[session_type].check_subscription()
                    break
                except TidalAuthError as e:
                    print(f'{e}')
                    confirm = input('Do you want to create a new session? [Y/n]: ')

                    if confirm.upper() == 'N':
                        print('Exiting...')
                        exit()

                    # Create a new session finally
                    if session_type == SessionType.TV.name:
                        sessions[session_type].auth()
                    else:
                        print('Tidal: Enter your Tidal username and password:')
                        username = input('Username: ')
                        password = getpass('Password: ')
                        sessions[session_type].auth(username, password)

                    module_controller.temporary_settings_controller.set(session_type,
                                                                        sessions[session_type].get_storage())

        self.session: TidalApi = TidalApi(sessions)

        # Track cache for credits
        self.track_cache = {}
        # Album cache
        self.album_cache = {}

    def generate_artwork_url(self, cover_id, max_size=1280):
        # not the best idea, but it rounds the self.cover_size to the nearest number in supported_sizes, 1281 is needed
        # for the "uncompressed" cover
        supported_sizes = [80, 160, 320, 480, 640, 1080, 1280, 1281]
        best_size = min(supported_sizes, key=lambda x: abs(x - self.cover_size))
        # only supports 80x80, 160x160, 320x320, 480x480, 640x640, 1080x1080 and 1280x1280 only for non playlists
        # return "uncompressed" cover if self.cover_resolution > max_size
        image_name = '{0}x{0}.jpg'.format(best_size) if best_size <= max_size else 'origin.jpg'
        return f'https://resources.tidal.com/images/{cover_id.replace("-", "/")}/{image_name}'

    @staticmethod
    def generate_animated_artwork_url(cover_id, size=1280):
        return 'https://resources.tidal.com/videos/{0}/{1}x{1}.mp4'.format(cover_id.replace('-', '/'), size)

    def search(self, query_type: DownloadTypeEnum, query: str, track_info: TrackInfo = None, limit: int = 20):
        results = self.session.get_search_data(query, limit=limit)

        items = []
        for i in results[query_type.name + 's']['items']:
            if query_type is DownloadTypeEnum.artist:
                name = i['name']
                artists = None
                year = None
            elif query_type is DownloadTypeEnum.playlist:
                name = i['title']
                artists = [i['creator']['name']]
                year = ""
            elif query_type is DownloadTypeEnum.track:
                name = i['title']
                artists = [j['name'] for j in i['artists']]
                # Getting the year from the album?
                year = i['album']['releaseDate'][:4]
            elif query_type is DownloadTypeEnum.album:
                name = i['title']
                artists = [j['name'] for j in i['artists']]
                year = i['releaseDate'][:4]
            else:
                raise Exception('Query type is invalid')

            additional = None
            if query_type is not DownloadTypeEnum.artist:
                if i['audioModes'] == ['DOLBY_ATMOS']:
                    additional = "Dolby Atmos"
                elif i['audioModes'] == ['SONY_360RA']:
                    additional = "360 Reality Audio"
                elif i['audioQuality'] == 'HI_RES':
                    additional = "MQA"
                else:
                    additional = 'HiFi'

            item = SearchResult(
                name=name,
                artists=artists,
                year=year,
                result_id=str(i['id']),
                explicit=bool(i['explicit']) if 'explicit' in i else None,
                additional=[additional] if additional else None
            )

            items.append(item)

        return items

    def get_track_info(self, track_id: str, quality_tier: QualityEnum, codec_options: CodecOptions) -> TrackInfo:
        track_data = self.session.get_track(track_id)

        album_id = str(track_data['album']['id'])

        # Check if album is already in album cache, add it
        if album_id in self.album_cache:
            album_data = self.album_cache[album_id]
        else:
            album_data = self.session.get_album(album_id)

        # Get Sony 360RA and switch to mobile session
        if track_data['audioModes'] == ['SONY_360RA'] and SessionType.MOBILE.name in self.available_sessions:
            self.session.default = SessionType.MOBILE
        else:
            self.session.default = SessionType.TV

        stream_data = self.session.get_stream_url(track_id, self.quality_parse[quality_tier])

        manifest = json.loads(base64.b64decode(stream_data['manifest']))
        track_codec = CodecEnum['AAC' if 'mp4a' in manifest['codecs'] else manifest['codecs'].upper()]

        if not codec_data[track_codec].spatial:
            if not codec_options.proprietary_codecs and codec_data[track_codec].proprietary:
                # TODO: use indents from music_downloader.py
                print(f'\t\tProprietary codecs are disabled, if you want to download {track_codec.name}, '
                      f'set "proprietary_codecs": true')
                stream_data = self.session.get_stream_url(track_id, 'LOSSLESS')

                manifest = json.loads(base64.b64decode(stream_data['manifest']))
                track_codec = CodecEnum['AAC' if 'mp4a' in manifest['codecs'] else manifest['codecs'].upper()]

        track_name = track_data["title"]
        track_name += f' ({track_data["version"]})' if track_data['version'] else ''

        track_info = TrackInfo(
            name=track_name,
            album=album_data['title'],
            album_id=album_id,
            artists=[a['name'] for a in track_data['artists']],
            artist_id=track_data['artist']['id'],
            release_year=track_data['streamStartDate'][:4],
            # TODO: Get correct bit_depth and sample_rate for MQA, even possible?
            bit_depth=24 if track_codec in [CodecEnum.MQA, CodecEnum.EAC3, CodecEnum.MHA1] else 16,
            sample_rate=48 if track_codec in [CodecEnum.EAC3, CodecEnum.MHA1] else 44.1,
            cover_url=self.generate_artwork_url(track_data['album']['cover']),
            tags=self.convert_tags(track_data, album_data),
            codec=track_codec,
            download_extra_kwargs={'file_url': manifest['urls'][0]}
        )

        if not codec_options.spatial_codecs and codec_data[track_codec].spatial:
            track_info.error = 'Spatial codecs are disabled, if you want to download it, set "spatial_codecs": true'

        return track_info

    @staticmethod
    def get_track_download(file_url: str) -> TrackDownloadInfo:
        return TrackDownloadInfo(download_type=DownloadEnum.URL, file_url=file_url)

    def get_track_lyrics(self, track_id: str) -> LyricsInfo:
        embedded, synced = None, None

        lyrics_data = self.session.get_lyrics(track_id)

        if 'lyrics' in lyrics_data:
            embedded = lyrics_data['lyrics']

        if 'subtitles' in lyrics_data:
            synced = lyrics_data['subtitles']

        return LyricsInfo(
            embedded=embedded,
            synced=synced
        )

    def get_playlist_info(self, playlist_id: str) -> PlaylistInfo:
        playlist_data = self.session.get_playlist(playlist_id)
        playlist_tracks = self.session.get_playlist_items(playlist_id)

        tracks = [track['item']['id'] for track in playlist_tracks['items'] if track['type'] == 'track']

        if 'name' in playlist_data['creator']:
            creator_name = playlist_data['creator']['name']
        elif playlist_data['creator']['id'] == 0:
            creator_name = 'TIDAL'
        else:
            creator_name = 'Unknown'

        playlist_info = PlaylistInfo(
            name=playlist_data['title'],
            creator=creator_name,
            tracks=tracks,
            # TODO: Use playlist creation date or lastUpdated?
            release_year=playlist_data['created'][:4],
            creator_id=playlist_data['creator']['id'],
            cover_url=self.generate_artwork_url(playlist_data['squareImage'], max_size=1080)
        )

        return playlist_info

    def get_album_info(self, album_id):
        # Check if album is already in album cache, add it
        if album_id in self.album_cache:
            album_data = self.album_cache[album_id]
        else:
            album_data = self.session.get_album(album_id)

        # Get all album tracks with corresponding credits
        tracks_data = self.session.get_album_contributors(album_id)

        tracks = [str(track['item']['id']) for track in tracks_data['items']]

        # Cache all track (+credits) in track_cache
        self.track_cache.update({str(track['item']['id']): track for track in tracks_data['items']})

        if album_data['audioModes'] == ['DOLBY_ATMOS']:
            quality = 'Dolby Atmos'
        elif album_data['audioModes'] == ['SONY_360RA']:
            quality = '360'
        elif album_data['audioQuality'] == 'HI_RES':
            quality = 'M'
        else:
            quality = None

        album_info = AlbumInfo(
            name=album_data['title'],
            release_year=album_data['releaseDate'][:4],
            explicit=album_data['explicit'],
            quality=quality,
            cover_url=self.generate_artwork_url(album_data['cover']),
            animated_cover_url=self.generate_animated_artwork_url(album_data['videoCover']) if album_data[
                'videoCover'] else None,
            artist=album_data['artist']['name'],
            artist_id=album_data['artist']['id'],
            tracks=tracks,
        )

        return album_info

    def get_artist_info(self, artist_id: str, get_credited_albums: bool) -> ArtistInfo:
        artist_data = self.session.get_artist(artist_id)

        artist_albums = self.session.get_artist_albums(artist_id)['items']
        artist_singles = self.session.get_artist_albums_ep_singles(artist_id)['items']

        # Only works with a mobile session, annoying, never do this again
        credit_albums = []
        if get_credited_albums and SessionType.MOBILE.name in self.available_sessions:
            self.session.default = SessionType.MOBILE
            credited_albums_page = self.session.get_page('contributor', params={'artistId': artist_id})

            # This is so retarded
            page_list = credited_albums_page['rows'][-1]['modules'][0]['pagedList']
            total_items = page_list['totalNumberOfItems']
            more_items_link = page_list['dataApiPath'][6:]

            # Now fetch all the found total_items
            items = []
            for offset in range(0, total_items // 50 + 1):
                print(f'Fetching {offset * 50}/{total_items}', end='\r')
                items += self.session.get_page(more_items_link, params={'limit': 50, 'offset': offset * 50})['items']

            credit_albums = [item['item']['album'] for item in items]
            self.session.default = SessionType.TV

        albums = [str(album['id']) for album in artist_albums + artist_singles + credit_albums]

        artist_info = ArtistInfo(
            name=artist_data['name'],
            albums=albums
        )

        return artist_info

    def get_track_credits(self, track_id: str) -> Optional[list]:
        credits_dict = {}

        # Fetch credits from cache if not fetch those credits
        if track_id in self.track_cache:
            track_contributors = self.track_cache[track_id]['credits']

            for contributor in track_contributors:
                credits_dict[contributor['type']] = [c['name'] for c in contributor['contributors']]
        else:
            track_contributors = self.session.get_track_contributors(track_id)['items']

            if len(track_contributors) > 0:
                for contributor in track_contributors:
                    # Check if the dict contains no list, create one
                    if contributor['role'] not in credits_dict:
                        credits_dict[contributor['role']] = []

                    credits_dict[contributor['role']].append(contributor['name'])

        if len(credits_dict) > 0:
            # Convert the dictionary back to a list of CreditsInfo
            return [CreditsInfo(sanitise_name(k), v) for k, v in credits_dict.items()]
        return None

    @staticmethod
    def convert_tags(track_data: dict, album_data: dict) -> Tags:
        track_name = track_data["title"]
        track_name += f' ({track_data["version"]})' if track_data['version'] else ''

        tags = Tags(
            album_artist=album_data['artist']['name'],
            track_number=track_data['trackNumber'],
            total_tracks=album_data['numberOfTracks'],
            disc_number=track_data['volumeNumber'],
            total_discs=album_data['numberOfVolumes'],
            isrc=track_data['isrc'],
            copyright=track_data['copyright'],
            replay_gain=track_data['replayGain'],
            replay_peak=track_data['peak']
        )

        return tags
