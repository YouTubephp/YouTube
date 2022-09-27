import re

from .common import InfoExtractor
from ..utils import make_archive_id, get_elements_text_and_html_by_attribute


class HTML5MediaEmbedIE(InfoExtractor):
    _VALID_URL = False
    IE_NAME = 'html5'
    _WEBPAGE_TESTS = [
        {
            'url': 'https://html.com/media/',
            'info_dict': {
                'title': 'HTML5 Media',
                'description': 'md5:933b2d02ceffe7a7a0f3c8326d91cc2a',
            },
            'playlist_count': 2
        }
    ]

    def _extract_from_webpage(self, url, webpage):
        video_id, title = self._generic_id(url), self._generic_title(url)
        entries = self._parse_html5_media_entries(url, webpage, video_id, m3u8_id='hls') or []
        for num, entry in enumerate(entries, start=1):
            entry.update({
                'id': f'{video_id}-{num}',
                'title': f'{title} ({num})',
                '_old_archive_ids': [
                    make_archive_id('generic', f'{video_id}-{num}' if len(entries) > 1 else video_id),
                ],
            })
            self._sort_formats(entry['formats'])
            yield entry

from yt_dlp.jsinterp import JSInterpreter

class MbMiniPlayerEmbedIE(InfoExtractor):
    _VALID_URL = False
    def _extract_from_webpage(self, url, webpage):
        js = JSInterpreter(webpage).extract_function_code('initializeMiniAudioPlayer')
        query_params = self._search_regex(r'jQuery([^;]+)\.mb_miniPlayer', js[1], 'query params')
        file_exts = re.findall(r'a\[href\*=\'\.([a-zA-Z0-9]+)\'', query_params)
        css_exclude = re.findall(r'\.not\("([^"]+)', query_params)
        a = list(get_elements_text_and_html_by_attribute(f'href', rf'(?:[^\"\']+\.(?:{"|".join(file_exts)}))', webpage, escape_value=False))

        print("two")
        pass
