import json
import subprocess

import pytest

from app.bytedance import collect_candidates, safe_media_headers
from app.hls import parse_master_playlist
from app.models import Platform, StreamCandidate
from app.normalizer import normalize
from app.platforms.tiktok import _codec, _resolution
from app.probe import probe_candidate
from app.quality import mark_recommended, sort_best
from app.stream_family import add_family_hypotheses, derive_base_candidate, stream_family


def test_platforms():
    assert normalize('https://www.tiktok.com/@abc/live')[0] == Platform.TIKTOK
    assert normalize('https://live.douyin.com/123')[0] == Platform.DOUYIN
    assert normalize('https://www.facebook.com/watch/?v=123')[0] == Platform.FACEBOOK


def test_quality_prefers_fps_at_same_resolution():
    a = StreamCandidate(id='a', protocol='flv', url='https://a', width=1920, height=1080, fps=30, bitrate=4_000_000)
    b = StreamCandidate(id='b', protocol='flv', url='https://b', width=1920, height=1080, fps=60, bitrate=3_000_000)
    assert sort_best([a, b])[0].id == 'b'


def test_quality_prefers_actual_resolution_over_label():
    fake_origin = StreamCandidate(id='a', protocol='flv', url='https://a', platform_quality='origin', width=640, height=1280, bitrate=2_000_000, is_original=True)
    real_1080 = StreamCandidate(id='b', protocol='flv', url='https://b', platform_quality='hd', width=1080, height=1920, bitrate=5_000_000)
    assert sort_best([fake_origin, real_1080])[0].id == 'b'


def test_recommended_uses_verified_max_not_transport_label():
    hls = StreamCandidate(id='h', protocol='hls', url='https://h', width=1080, height=1920, fps=60, bitrate=7_000_000, verified=True)
    flv = StreamCandidate(id='f', protocol='flv', url='https://f', width=1080, height=1920, fps=60, bitrate=6_000_000, verified=True, video_codec='h264')
    out = mark_recommended([hls, flv])
    assert next(x for x in out if x.recommended).id == 'h'


def test_tiktok_meta_parsers():
    assert _resolution('1080x1920') == (1080, 1920)
    assert _resolution('1920*1080') == (1920, 1080)
    assert _codec('bytevc1') == 'h265'
    assert _codec('H264') == 'h264'


def test_parse_webcast_original_flv():
    from app.platforms.tiktok import TikTokResolver
    resolver = TikTokResolver(None)
    payload = {
        'stream_url': {
            'live_core_sdk_data': {
                'pull_data': {
                    'stream_data': json.dumps({
                        'data': {
                            'origin': {
                                'main': {
                                    'flv': 'https://cdn.example/origin.flv',
                                    'sdk_params': json.dumps({
                                        'VCodec': 'h264', 'vbitrate': 6500000,
                                        'resolution': '1080x1920', 'fps': 60,
                                    }),
                                }
                            }
                        }
                    })
                }
            }
        }
    }
    streams = resolver._parse_webcast_formats(payload, {'User-Agent': 'test'})
    assert streams[0].is_original is True
    assert streams[0].width == 1080 and streams[0].height == 1920
    assert streams[0].fps == 60
    assert streams[0].bitrate == 6500000


def test_max_source_allows_hevc_to_win_at_same_geometry():
    h265 = StreamCandidate(id='hevc', protocol='flv', url='https://hevc', width=1080, height=1920, fps=60, bitrate=8_000_000, verified=True, video_codec='hevc')
    h264 = StreamCandidate(id='avc', protocol='flv', url='https://avc', width=1080, height=1920, fps=60, bitrate=6_000_000, verified=True, video_codec='h264')
    out = mark_recommended([h265, h264])
    assert next(x for x in out if x.recommended).id == 'hevc'


def test_unverified_derived_never_beats_verified_lower_stream():
    guessed_4k = StreamCandidate(id='guess', protocol='flv', url='https://g', width=2160, height=3840, fps=60, bitrate=20_000_000, derived=True, verified=False)
    verified_720 = StreamCandidate(id='real', protocol='flv', url='https://r', width=720, height=1280, fps=30, bitrate=2_000_000, verified=True)
    out = mark_recommended([guessed_4k, verified_720])
    assert next(x for x in out if x.recommended).id == 'real'


def test_parse_webcast_generic_rtmp_as_origin():
    from app.platforms.tiktok import TikTokResolver
    resolver = TikTokResolver(None)
    payload = {
        'stream_url': {
            'rtmp_pull_url': 'https://cdn.example/origin.flv',
            'rtmp_pull_url_params': json.dumps({'VCodec': 'h264', 'resolution': '1080x1920', 'vbitrate': 6000000}),
            'extra': {'fps': 60, 'width': 1080, 'height': 1920, 'default_bitrate': 6000000},
            'live_core_sdk_data': {'pull_data': {'stream_data': '{}'}},
        }
    }
    streams = resolver._parse_webcast_formats(payload, {'User-Agent': 'test'})
    origin = next(s for s in streams if s.id == 'tt_pull_rtmp_origin')
    assert origin.is_original is True
    assert origin.width == 1080 and origin.height == 1920
    assert origin.fps == 60


def test_parse_options_enriches_stream_data():
    from app.platforms.tiktok import TikTokResolver
    resolver = TikTokResolver(None)
    payload = {
        'stream_url': {
            'live_core_sdk_data': {
                'pull_data': {
                    'options': {'qualities': [
                        {'sdk_key': 'origin', 'name': 'ORIGION', 'resolution': '1080x1920', 'v_codec': 'h264', 'fps': 60, 'bitrate': 6500000}
                    ]},
                    'stream_data': json.dumps({'data': {'origin': {'main': {'flv': 'https://cdn.example/o.flv', 'sdk_params': '{}'}}}}),
                }
            }
        }
    }
    streams = resolver._parse_webcast_formats(payload, {'User-Agent': 'test'})
    c = next(s for s in streams if s.id == 'tt_webcast_flv_origin')
    assert (c.width, c.height, c.fps, c.bitrate, c.video_codec) == (1080, 1920, 60, 6500000, 'h264')


def test_parse_live_detail_origin_hls():
    from app.platforms.tiktok import TikTokResolver
    resolver = TikTokResolver(None)
    detail = {'liveUrl': 'https://cdn.example/original.m3u8', 'title': 'Live title'}
    streams = resolver._parse_live_detail_formats(detail, {'User-Agent': 'test'})
    c = next(s for s in streams if s.id == 'tt_live_detail_origin')
    assert c.protocol == 'hls'
    assert c.is_original is True
    assert c.source == 'api.live.detail'


def test_web_params_use_full_desktop_shape():
    from app.platforms.tiktok import _web_params
    p = _web_params()
    assert p['device_platform'] == 'web_pc'
    assert p['channel'] == 'tiktok_web'
    assert p['screen_width'] == '1920'
    assert p['region'] == 'VN'


def test_bytedance_recursive_stream_data_enumerates_all_qualities():
    payload = {
        'live_core_sdk_data': {
            'pull_data': {
                'stream_data': json.dumps({
                    'data': {
                        'origin': {'main': {'flv': 'https://cdn.test/stream-1.flv', 'sdk_params': json.dumps({'resolution': '2160x3840', 'fps': 60, 'vbitrate': 16000000, 'VCodec': 'bytevc1'})}},
                        'hd': {'main': {'flv': 'https://cdn.test/stream-1_or4.flv', 'sdk_params': json.dumps({'resolution': '1080x1920', 'fps': 60, 'vbitrate': 6000000, 'VCodec': 'h264'})}},
                    }
                })
            }
        }
    }
    out = collect_candidates(payload, source='fixture')
    assert len(out) == 2
    best = sort_best(out)[0]
    assert (best.width, best.height, best.fps, best.video_codec) == (2160, 3840, 60, 'hevc')


def test_sensitive_headers_never_leave_backend():
    headers = safe_media_headers({
        'User-Agent': 'ua', 'Referer': 'https://x', 'Origin': 'https://x',
        'Cookie': 'sessionid=SECRET', 'Authorization': 'Bearer SECRET', 'X-CSRF-Token': 'SECRET',
    })
    assert headers['User-Agent'] == 'ua'
    assert 'Cookie' not in headers and 'Authorization' not in headers and 'X-CSRF-Token' not in headers


def test_hls_master_expands_4k60_variant():
    parent = StreamCandidate(id='m', protocol='hls', url='https://cdn.test/master.m3u8')
    text = '''#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720,FRAME-RATE=30,CODECS="avc1.64001f,mp4a.40.2"
720.m3u8
#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=14000000,RESOLUTION=3840x2160,FRAME-RATE=60,CODECS="hvc1.2.4.L153.B0,mp4a.40.2"
4k.m3u8
'''
    out = parse_master_playlist(text, parent.url, parent)
    best = sort_best(out)[0]
    assert best.url == 'https://cdn.test/4k.m3u8'
    assert (best.width, best.height, best.fps, best.video_codec) == (3840, 2160, 60.0, 'hevc')


def test_stream_family_derivation_is_conservative_and_unverified():
    c = StreamCandidate(id='x', protocol='flv', url='https://cdn.test/path/stream-7476883492858252054_or4.flv?token=abc', verified=True)
    family, suffix = stream_family(c.url)
    assert family == '7476883492858252054' and suffix == '_or4'
    derived = derive_base_candidate(c)
    assert derived is not None
    assert derived.url == 'https://cdn.test/path/stream-7476883492858252054.flv?token=abc'
    assert derived.derived is True and derived.verified is False and derived.recommended is False


def test_stream_family_does_not_guess_unknown_suffix():
    c = StreamCandidate(id='x', protocol='flv', url='https://cdn.test/stream-123_customweird.flv')
    assert derive_base_candidate(c) is None
    assert len(add_family_hypotheses([c])) == 1


@pytest.mark.asyncio
async def test_ffprobe_overrides_wrong_metadata_with_actual_media(tmp_path):
    sample = tmp_path / 'sample.mp4'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'color=size=1280x720:rate=60:color=black',
        '-t', '0.25', '-c:v', 'mpeg4', '-q:v', '5', str(sample),
    ], check=True)
    wrong = StreamCandidate(
        id='wrong', protocol='http', url=str(sample),
        width=3840, height=2160, fps=30, bitrate=99_000_000,
    )
    probed = await probe_candidate(wrong, deep=True)
    assert probed.verified is True
    assert (probed.width, probed.height) == (1280, 720)
    assert 59 <= (probed.fps or 0) <= 61
    assert probed.video_codec == 'mpeg4'


def test_browser_observer_media_detection_and_capture_filter():
    from app.browser_observer import is_media_url, should_capture_response
    assert is_media_url('https://pull.example/stream-123.flv?x=1')
    assert is_media_url('https://pull.example/master.m3u8')
    assert is_media_url('https://pull.example/live.mpd')
    assert not is_media_url('https://example.com/avatar.jpg')
    assert should_capture_response('https://live.douyin.com/webcast/room/web/enter/', 'application/json')


def test_html_page_state_discovers_unsuffixed_hevc_candidate():
    html = r'''<script>window.STATE={"live_core_sdk_data":{"pull_data":{"stream_data":"{\\"data\\":{\\"origin\\":{\\"main\\":{\\"flv\\":\\"https:\\\/\\\/cdn.test\\\/stream-6950459845752.flv?codec=bytevc1\\u0026x=1\\",\\"sdk_params\\":\\"{\\\\\\"resolution\\\\\\":\\\\\\"2160x3840\\\\\\",\\\\\\"fps\\\\\\":60,\\\\\\"VCodec\\\\\\":\\\\\\"bytevc1\\\\\\"}\\"}}}}"}}};</script>'''
    out = collect_candidates(html, source='page', provenance='PAGE_STATE', observed_by_player=True)
    assert out
    c = out[0]
    assert 'stream-6950459845752.flv' in c.url
    assert c.observed_by_player is True


def test_stream_family_keeps_query_token_when_deriving_base():
    c = StreamCandidate(id='x', protocol='flv', url='https://cdn.test/stream-999_or4.flv?k=abc&t=123')
    d = derive_base_candidate(c)
    assert d is not None
    assert d.url == 'https://cdn.test/stream-999.flv?k=abc&t=123'


def test_quality_max_source_has_no_1080_ceiling():
    q1080 = StreamCandidate(id='1080', protocol='flv', url='https://1', width=1920, height=1080, fps=60, bitrate=8_000_000, verified=True)
    q2k = StreamCandidate(id='2k', protocol='flv', url='https://2', width=2560, height=1440, fps=60, bitrate=12_000_000, verified=True)
    q4k = StreamCandidate(id='4k', protocol='hls', url='https://4', width=3840, height=2160, fps=60, bitrate=18_000_000, verified=True)
    out = mark_recommended([q1080, q2k, q4k])
    assert next(c for c in out if c.recommended).id == '4k'


def test_player_observed_verified_stream_beats_same_media_api_tie():
    api = StreamCandidate(id='api', protocol='flv', url='https://a', width=1920, height=1080, fps=60, bitrate=6_000_000, verified=True, observed_by_player=False)
    player = StreamCandidate(id='player', protocol='flv', url='https://p', width=1920, height=1080, fps=60, bitrate=6_000_000, verified=True, observed_by_player=True)
    assert sort_best([api, player])[0].id == 'player'


def test_compact_streams_keeps_winner_and_distinct_visual_choices():
    from app.presentation import compact_streams
    streams = [
        StreamCandidate(id='winner', protocol='flv', url='https://1', width=1080, height=1920, fps=60, bitrate=8_000_000, video_codec='hevc', verified=True, recommended=True),
        StreamCandidate(id='mirror', protocol='hls', url='https://2', width=1080, height=1920, fps=60, bitrate=7_500_000, video_codec='hevc', verified=True),
        StreamCandidate(id='avc', protocol='flv', url='https://3', width=1080, height=1920, fps=30, bitrate=6_000_000, video_codec='h264', verified=True),
        StreamCandidate(id='720', protocol='flv', url='https://4', width=720, height=1280, fps=30, bitrate=2_000_000, video_codec='h264', verified=True),
        StreamCandidate(id='480', protocol='hls', url='https://5', width=480, height=854, fps=30, bitrate=1_000_000, video_codec='h264', verified=True),
        StreamCandidate(id='360', protocol='hls', url='https://6', width=360, height=640, fps=30, bitrate=700_000, video_codec='h264', verified=True),
    ]
    out = compact_streams(streams, limit=5)
    assert out[0].id == 'winner'
    assert len(out) == 5
    assert 'mirror' not in {c.id for c in out}  # same visual quality, unnecessary phone card
    assert {'avc', '720'}.issubset({c.id for c in out})


def test_probe_selection_is_capped_and_keeps_player_discovery():
    from app.probe import _select_probe_set
    streams = []
    for i in range(20):
        streams.append(StreamCandidate(
            id=f'api{i}', protocol='flv', url=f'https://api/{i}.flv',
            width=1080 if i < 10 else 720, height=1920 if i < 10 else 1280,
            fps=30, bitrate=5_000_000 - i * 10_000, video_codec='h264',
        ))
    streams.append(StreamCandidate(
        id='player', protocol='flv', url='https://player/source.flv',
        width=1080, height=1920, fps=60, bitrate=6_000_000,
        video_codec='hevc', observed_by_player=True,
    ))
    selected = _select_probe_set(streams, 8)
    assert len(selected) <= 8
    assert any(c.id == 'player' for c in selected)

@pytest.mark.asyncio
async def test_tiktok_discovery_runs_independent_paths_in_parallel(monkeypatch):
    import asyncio
    import time
    import app.platforms.tiktok as tt
    from app.browser_observer import BrowserObservation

    resolver = tt.TikTokResolver(None)
    independent_starts = []

    async def profile(username, headers):
        independent_starts.append(time.perf_counter())
        await asyncio.sleep(0.08)
        return {'room_id': '123', 'creator_name': username, 'source': 'TEST'}

    async def room_api(username, live_url, headers):
        independent_starts.append(time.perf_counter())
        await asyncio.sleep(0.08)
        return {'title': 'x', 'room_id': '123', 'streams': [
            StreamCandidate(id='api', protocol='flv', url='https://cdn/a.flv', width=1080, height=1920, fps=30, bitrate=4_000_000, video_codec='h264')
        ]}

    async def webcast(room_id, username, headers):
        await asyncio.sleep(0.08)
        return None

    async def detail(room_id, username, headers):
        await asyncio.sleep(0.08)
        return None

    async def observer(url):
        independent_starts.append(time.perf_counter())
        await asyncio.sleep(0.08)
        return BrowserObservation(candidates=[
            StreamCandidate(id='player', protocol='flv', url='https://cdn/p.flv', width=1080, height=1920, fps=30, bitrate=5_000_000, video_codec='hevc', observed_by_player=True)
        ], page_state_candidates=1)

    async def expand(client, streams):
        return streams

    async def probe(streams, **kwargs):
        return [c.model_copy(update={'verified': True}) for c in streams]

    monkeypatch.setattr(resolver, '_profile_metadata', profile)
    monkeypatch.setattr(resolver, '_room_api', room_api)
    monkeypatch.setattr(resolver, '_webcast_room_info', webcast)
    monkeypatch.setattr(resolver, '_live_detail', detail)
    monkeypatch.setattr(tt, 'observe_player', observer)
    monkeypatch.setattr(tt, 'expand_hls_candidates', expand)
    monkeypatch.setattr(tt, 'probe_best_candidates', probe)

    result = await resolver.resolve('https://www.tiktok.com/@abc/live')
    assert result.state.value == 'LIVE'
    assert next(c for c in result.streams if c.recommended).id == 'player'
    assert len(independent_starts) == 3
    assert max(independent_starts) - min(independent_starts) < 0.05

@pytest.mark.asyncio
async def test_douyin_page_and_browser_discovery_overlap(monkeypatch):
    import asyncio
    import time
    import app.platforms.douyin as dy
    from app.browser_observer import BrowserObservation

    class FakeResponse:
        url = 'https://live.douyin.com/123'
        text = '{"title":"x","nickname":"n","stream":"https://cdn.test/a.flv"}'

    class FakeClient:
        async def get(self, *args, **kwargs):
            await asyncio.sleep(0.08)
            return FakeResponse()

    async def observer(url):
        await asyncio.sleep(0.08)
        return BrowserObservation(candidates=[
            StreamCandidate(id='player', protocol='flv', url='https://cdn.test/p.flv', width=1080, height=1920, fps=60, bitrate=8_000_000, video_codec='hevc', observed_by_player=True)
        ], page_state_candidates=1)

    async def expand(client, streams):
        return streams

    async def probe(streams, **kwargs):
        return [c.model_copy(update={'verified': True}) for c in streams]

    monkeypatch.setattr(dy, 'observe_player', observer)
    monkeypatch.setattr(dy, 'expand_hls_candidates', expand)
    monkeypatch.setattr(dy, 'probe_best_candidates', probe)

    started = time.perf_counter()
    result = await dy.DouyinResolver(FakeClient()).resolve('https://live.douyin.com/123')
    elapsed = time.perf_counter() - started
    assert result.state.value == 'LIVE'
    assert elapsed < 0.16


def test_absurd_fps_is_sanitized_everywhere():
    from app.probe import _fps
    assert StreamCandidate(id='x', protocol='flv', url='https://x', fps=1000).fps is None
    assert StreamCandidate(id='x', protocol='flv', url='https://x', fps=120).fps == 120
    assert _fps('1000/1') is None
    assert _fps('120/1') == 120.0


def test_tiktok_short_link_is_recognized_from_share_text():
    from app.normalizer import extract_url, detect_platform
    raw = 'Xem LIVE này https://vm.tiktok.com/ZMtest123/ nhé'
    url = extract_url(raw)
    assert url == 'https://vm.tiktok.com/ZMtest123/'
    assert detect_platform(url) == Platform.TIKTOK
