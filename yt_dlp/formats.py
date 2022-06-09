import collections
import enum
import functools
import itertools
import re

from .utils import (
    Namespace,
    apply_filter,
    determine_protocol,
    orderedSet,
    traverse_obj,
)


def _filter_each(f, l):
    return filter(None, map(tuple, (filter(f, x) for x in l)))


def _debug_suitable_formats(func):
    """Only for debugging purposes"""
    from .utils import filter_dict

    def wrapper(self, formats, ctx):
        print(self, func.__qualname__)
        opts = filter_dict(ctx.__dict__)
        opts.pop('check_format')
        opts.pop('info_dict')
        print('   ', str(Namespace(**opts))[9:])
        print('   ', [f['format_id'] for f in formats])
        ret = list(map(list, func(self, formats, ctx)))
        print(' =>', '\n    '.join(str([f['format_id'] for f in x]) for x in ret[:10]))
        if len(ret) > 10:
            print('    ...')
        return ret
    return wrapper


def format_resolution(format, default='unknown'):
    if FormatType.of(format) == FormatType.Audio:
        return 'audio only'
    elif format.get('resolution') is not None:
        return format['resolution']
    elif format.get('width') and format.get('height'):
        return '%dx%d' % (format['width'], format['height'])
    elif format.get('height'):
        return '%sp' % format['height']
    elif format.get('width'):
        return '%dx?' % format['width']
    return default


class FormatType(enum.Enum):
    Video = enum.auto()
    Audio = enum.auto()
    Merged = enum.auto()
    Storyboards = enum.auto()

    @classmethod
    def of(cls, format):
        if format.get('vcodec') == 'none':
            return cls.Storyboards if format.get('acodec') == 'none' else cls.Audio
        return cls.Video if format.get('acodec') == 'none' else cls.Merged

    def isin(self, format):
        type_ = self.of(format)
        return type_ == self or (type_ == self.Merged and self != self.Storyboards)


ALL_TOKENS = Namespace(
    TAKE_FIRST='/',
    TAKE_ALL=',',
    MERGE_OPTIONAL='+?',
    MERGE='+',
    GROUP_START='(',
    GROUP_END=')',
    FILTER_START='[',
    FILTER_END=']',
)


class TokenIterator:
    counter = 0

    def __init__(self, spec, allowed_tokens):
        self.spec = spec
        self.tokens = tuple(self.tokenize(spec, allowed_tokens))

    @staticmethod
    def _current_token(spec, allowed_tokens):
        return next((token for token in allowed_tokens if spec.startswith(token)), None)

    @classmethod
    def tokenize(cls, spec, allowed_tokens):
        currently_allowed_tokens = allowed_tokens
        while spec:
            name = ''.join(itertools.takewhile(lambda x: not cls._current_token(x, currently_allowed_tokens), spec))
            spec = spec[len(name):]

            token = cls._current_token(spec, currently_allowed_tokens) or ''
            spec = spec[len(token):]

            currently_allowed_tokens = [ALL_TOKENS.FILTER_END] if token == ALL_TOKENS.FILTER_START else allowed_tokens
            yield from filter(None, (name.strip(), token))

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return self.tokens[self.counter]
        except IndexError:
            raise StopIteration()
        finally:
            self.counter += 1

    @property
    def position(self):
        return sum(map(len, self.tokens[:self.counter - 1])) if self.counter else -1

    def restore_token(self):
        self.counter -= 1

    def SyntaxError(self, note):
        return SyntaxError(f'Invalid format specification: {note}\n\t{self.spec}\n\t{" " * (self.position - 1)}^')


def decompose_formats(*formats, ctx=None, optional=False):
    needs_audio, needs_video = True, True
    for f in itertools.chain.from_iterable(f.get('requested_formats', [f]) for f in formats):
        ret = False
        if FormatType.Audio.isin(f) and needs_audio:
            ret, needs_audio = True, not optional and (not ctx or ctx.allow_multiple_audio_streams)
        if FormatType.Video.isin(f) and needs_video:
            ret, needs_video = True, not optional and (not ctx or ctx.allow_multiple_video_streams)
        if ret:
            yield f


def merge_formats(formats, ctx=None, optional=False):
    formats = list(decompose_formats(*formats, ctx=ctx, optional=optional))
    if len(formats) == 1:
        return formats[0]

    video_fmts = list(filter(FormatType.Video.isin, formats))
    audio_fmts = list(filter(FormatType.Audio.isin, formats))

    the_only_video = video_fmts[0] if len(video_fmts) == 1 else {}
    the_only_audio = audio_fmts[0] if len(audio_fmts) == 1 else {}

    filtered = lambda *keys: filter(None, (traverse_obj(f, *keys) for f in formats))

    return {
        'ext': next(filter(None, (
            ctx and ctx.merge_output_format,
            the_only_video.get('ext'),
            not video_fmts and the_only_audio.get('ext'),
            'mkv',
        ))),
        'requested_formats': formats,
        'format': '+'.join(filtered('format')),
        'format_id': '+'.join(filtered('format_id')),
        'protocol': '+'.join(map(determine_protocol, formats)),
        'language': '+'.join(orderedSet(filtered('language'))) or None,
        'format_note': '+'.join(orderedSet(filtered('format_note'))) or None,
        'filesize_approx': sum(filtered('filesize', 'filesize_approx')) or None,
        'tbr': sum(filtered('tbr', 'vbr', 'abr')),
        'width': the_only_video.get('width'),
        'height': the_only_video.get('height'),
        'resolution': format_resolution(the_only_video),
        'fps': the_only_video.get('fps'),
        'dynamic_range': the_only_video.get('dynamic_range'),
        'vcodec': the_only_video.get('vcodec'),
        'vbr': the_only_video.get('vbr'),
        'stretched_ratio': the_only_video.get('stretched_ratio'),
        'acodec': the_only_audio.get('acodec'),
        'abr': the_only_audio.get('abr'),
        'asr': the_only_audio.get('asr'),
    }


class FormatSelector:
    """Base class for format selectors"""

    def __init__(self):
        raise NotImplementedError('Must be defined by subclasses')

    def suitable_formats(self, formats, ctx):
        """
        Get all formats matching the selector ignoring filters.
        See "process" for specification of the return value
        """
        raise NotImplementedError('Must be defined by subclasses')

    def apply(self, formats, ctx):
        """Apply the selector to given formats"""
        return next(_filter_each(ctx.check_format, self.process(formats[::-1], ctx)), [])

    #@_debug_suitable_formats
    def process(self, formats, ctx):
        """
        Get all formats matching the selector
        @returns   [(A, B), (C, D)]
                => (A and B) OR (C and D)
        """
        return _filter_each(functools.partial(self._match_filters, ctx=ctx), self.suitable_formats(formats, ctx))

    def add_filter(self, filter):
        """Add a filter to the selector"""
        func = functools.partial(apply_filter, filter)
        try:
            func({})
        except ValueError:
            raise SyntaxError(f'Invalid filter specification: {filter}')
        self.filters.append(func)
        self._kwargs.setdefault('filters', []).append(filter)

    def _match_filters(self, format, ctx):
        dct = collections.ChainMap(format, ctx.info_dict)
        return all(func(dct) for func in self.filters)

    def _initialize(self, **kwargs):
        self._kwargs = kwargs
        self.filters = []

    def __getattr__(self, name):
        try:
            return self._kwargs[name]
        except KeyError:
            raise AttributeError(f'{self.__class__.__qualname__} object has no attribute {name}')

    def __repr__(self):
        return f'{self.__class__.__qualname__}({", ".join(f"{k}={v!r}" for k, v in self._kwargs.items())})'

    def _evaluate_parts(self, formats, ctx):
        return (p.process(formats, ctx) for p in self.parts)


class FormatID(FormatSelector):
    """By format_id or ext"""

    EXTS = {
        'audio': {'m4a', 'mp3', 'ogg', 'aac'},
        'video': {'mp4', 'flv', 'webm', '3gp'},
        'storyboards': {'mhtml'},
    }

    def __init__(self, selector):
        self._initialize(selector=selector)

    def suitable_formats(self, formats, ctx):
        """
           [A, B]        # filter
        => [(A), (B)]
        """
        if self.selector in self.EXTS['audio']:
            cndn = lambda f: f.get('ext') == self.selector and FormatType.of(f) == FormatType.Audio
        elif self.selector in self.EXTS['video']:
            cndn = lambda f: f.get('ext') == self.selector and FormatType.of(f) == FormatType.Merged
            if not ctx.has_merged_format:
                # for compatibility with youtube-dl when there is no pre-merged format
                cndn = lambda f: f.get('ext') == self.selector and FormatType.of(f) == FormatType.Video
        elif self.selector in self.EXTS['storyboards']:
            cndn = lambda f: f.get('ext') == self.selector and FormatType.of(f) == FormatType.Storyboards
        else:
            cndn = lambda f: f.get('format_id') == self.selector

        return ([x] for x in filter(cndn, formats))


class SelectAll(FormatSelector):
    """all (v|a|) (*?)"""

    _ALLOW_STORYBOARDS = True

    def __init__(self, what):
        self._initialize(what=what or '*')

    def is_an_allowed_type(self, format):
        return {
            FormatType.Merged: not self.what or '*' in self.what,
            FormatType.Video: 'v' in self.what or self.what == '*',
            FormatType.Audio: 'a' in self.what or self.what == '*',
            FormatType.Storyboards: self.what == '*' and self._ALLOW_STORYBOARDS,
        }[FormatType.of(format)]

    def suitable_formats(self, formats, ctx):
        """
           [A, B]        # filter
        => [(A, B)]
        """
        if ctx.incomplete_formats and not self.what:
            # for extractors with incomplete formats (audio only (soundcloud)
            # or video only (imgur)) best/worst will fallback to
            # best/worst {video,audio}-only format
            yield formats
        else:
            yield filter(self.is_an_allowed_type, formats)


class MergeAll(SelectAll):
    """mergeall (v|a|) (*?)"""

    _ALLOW_STORYBOARDS = False

    def suitable_formats(self, formats, ctx):
        """
           [(A, B), (C, D)]  # SelectAll
        => [(A+B), (C+D)]
        """
        yield (merge_formats(f, ctx) for f in SelectAll.suitable_formats(self, formats, ctx))


class SelectBest(SelectAll):
    """(all)? (b|w) (v|a|) (*?) (.n)? ({field})?"""

    _ALLOW_STORYBOARDS = False

    def __init__(self, what='', n=1, field=''):
        self._initialize(what=what, n=n, field=field)

    def sort_formats(self, formats):
        return filter(None, (formats[::-1] if self.n < 0 else formats)[abs(self.n) - 1:])

    def suitable_formats(self, formats, ctx):
        """
           [(A, B, A2, B2), (C, D)]                      # SelectAll
        => [(A, A2), (B, B2), (C, D)]                    # groups.values()
        => [(A, B), (A, B2), (A2, B), (A2, B2), (C, D)]
        """
        for formats in SelectAll.suitable_formats(self, formats, ctx):
            groups = collections.defaultdict(list)
            for f in formats:
                groups[f.get(self.field)].append(f)
            yield from itertools.product(*map(self.sort_formats, groups.values()))


class MergeBest(SelectBest):
    """mergeall (b|w) (v|a|) (*?) (.n)? ({field})?"""

    def suitable_formats(self, formats, ctx):
        """
           [(A, B), (A, B2), (A2, B), (A2, B2), (C, D)]  # SelectBest
        => [(A+B), (A+B2), (A2+B), (A2+B2), (C+D)]
        """
        for f in SelectBest.suitable_formats(self, formats, ctx):
            yield [merge_formats(f, ctx)]


class TakeAll(FormatSelector):
    """TAKE_ALL (,)"""

    def __init__(self, *args):
        self._initialize(parts=args)

    def suitable_formats(self, formats, ctx):
        """
           ([(A, B), (C)], [(D), (E)])                             # _evaluate_parts
        => [((A, B), (D)), ((A, B), (E)), ((C), (D)), ((C), (E))]  # itertools.product
        => [(A, B, D), (A, B, E), (C, D), (C, E)]
        """
        return map(itertools.chain.from_iterable, itertools.product(*self._evaluate_parts(formats, ctx)))


class TakeFirst(FormatSelector):
    """TAKE_FIRST (/)"""

    def __init__(self, *args):
        self._initialize(parts=args)

    def suitable_formats(self, formats, ctx):
        """
           ([(A, B), (C)], [(D), (E)])  # _evaluate_parts
        => [(A, B), (C), (D), (E)]      # TakeFirst
        """
        return itertools.chain(*self._evaluate_parts(formats, ctx))


class Merge(FormatSelector):
    """MERGE (+), MERGE_OPTIONAL (+?)"""

    def __init__(self, *args, optional=False):
        self._initialize(parts=args, optional=optional)

    def suitable_formats(self, formats, ctx):
        """
           ([(A, B), (C)], [(D), (E)])                             # _evaluate_parts
        => [((A, B), (D)), ((A, B), (E)), ((C), (D)), ((C), (E))]  # itertools.product
        => [(A+D, B+D), (A+E, B+E), (C+D), (C+E)]
        """
        for x in itertools.product(*self._evaluate_parts(formats, ctx)):
            yield (merge_formats(f, ctx, self.optional) for f in itertools.product(*x))


class _SelectorMobj:
    SELECTOR_RE = re.compile(r'''(?x)
        (?P<merge>merge)?
        (?P<all>all)?
        (?P<which>b|w|best|worst)?
        (?P<what>v|a|video|audio)?
        (?P<containing>\*)?
        (?:\.(?P<n>[1-9]\d*))?
        (?:{(?P<field>\w+)})?
    ''')

    def __init__(self, string):
        mobj = self.SELECTOR_RE.fullmatch(string)
        self._dict = mobj.groupdict() if mobj else None

    def __bool__(self):
        return self._dict is not None

    def __getattr__(self, attr):
        return self._dict[attr] or ''

    @property
    def type(self):
        return f'{self.what[:1]}{self.containing}'

    @property
    def idx(self):
        return int(self.n or 1) * (-1 if self.which.startswith('w') else 1)

    @property
    def is_valid(self):
        return not any((
            self.all and (self.field or self.which or self.n),
            not self.all and not self.which,
            not self.all and self.merge and not self.field,
        ))


def parse_tokens(tokens, *, inside_merge_formats=False, inside_choice=False, inside_group=False):
    last_selector, current_selector = None, None
    for token in tokens:
        if token == ALL_TOKENS.FILTER_END:
            raise tokens.SynaxError(f'Unexpected {token!r}')
        elif token == ALL_TOKENS.GROUP_END:
            if not inside_group:
                tokens.restore_token()
            break
        elif inside_merge_formats and token in (ALL_TOKENS.TAKE_FIRST, ALL_TOKENS.TAKE_ALL):
            tokens.restore_token()
            break
        elif inside_choice and token == ALL_TOKENS.TAKE_ALL:
            tokens.restore_token()
            break
        elif token == ALL_TOKENS.TAKE_ALL:
            if not current_selector:
                raise tokens.SyntaxError(f'{token!r} must follow a format selector')
            if last_selector:
                last_selector = TakeAll(last_selector, current_selector)
            else:
                last_selector = current_selector
            current_selector = None
        elif token == ALL_TOKENS.TAKE_FIRST:
            if not current_selector:
                raise tokens.SyntaxError(f'{token!r} must follow a format selector')
            current_selector = TakeFirst(current_selector, parse_tokens(tokens, inside_choice=True))
        elif token == ALL_TOKENS.FILTER_START:
            if not current_selector:
                current_selector = SelectBest()
            filter = next(tokens, None)
            if not filter or filter in ALL_TOKENS:
                raise tokens.SyntaxError(f'{token!r} must follow a filter')
            current_selector.add_filter(filter)
            if next(tokens, None) != ALL_TOKENS.FILTER_END:
                raise tokens.SyntaxError(f'Filters must follow a {ALL_TOKENS.FILTER_END!r}')
        elif token == ALL_TOKENS.GROUP_START:
            if current_selector:
                raise tokens.SyntaxError(f'Unexpected {token!r}')
            current_selector = parse_tokens(tokens, inside_group=True)
        elif token in (ALL_TOKENS.MERGE, ALL_TOKENS.MERGE_OPTIONAL):
            if not current_selector:
                raise tokens.SyntaxError(f'Unexpected {token!r}')
            selector_2 = parse_tokens(tokens, inside_merge_formats=True)
            if not selector_2:
                raise tokens.SyntaxError('{token!r} must follow a format selector')
            current_selector = Merge(current_selector, selector_2, optional=token == ALL_TOKENS.MERGE_OPTIONAL)
        else:
            mobj = _SelectorMobj(token)
            if not mobj:
                current_selector = FormatID(token)
            elif not mobj.is_valid:
                raise tokens.SyntaxError('Invalid format selector {token!r}')
            elif mobj.which:
                op = MergeBest if mobj.merge else SelectBest
                current_selector = op(mobj.type, mobj.idx, mobj.field)
            else:
                op = MergeAll if mobj.merge else SelectAll
                current_selector = op(mobj.type)

    if current_selector and last_selector:
        return TakeAll(last_selector, current_selector)
    return current_selector or last_selector


def build_format_selector(format_spec, ydl):
    selector = parse_tokens(TokenIterator(format_spec, ALL_TOKENS))
    #print(selector)

    def func(formats, info_dict):
        checked_formats = {}

        def check_format(format):
            if ydl.params.get('check_formats') != 'selected':
                return True
            for f in decompose_formats(format):
                id_ = f['format_id']
                if id_ not in checked_formats:
                    checked_formats[id_] = ydl._checked_formats(f)
                if not checked_formats[id_]:
                    return False
            return True

        ctx = Namespace(
            merge_output_format=ydl.params.get('merge_output_format'),
            allow_multiple_audio_streams=ydl.params.get('allow_multiple_audio_streams'),
            allow_multiple_video_streams=ydl.params.get('allow_multiple_video_streams'),
            has_merged_format=FormatType.Merged in map(FormatType.of, formats),
            incomplete_formats=any(not next(filter(type_.isin, formats), None) for type_ in (FormatType.Video, FormatType.Audio)),
            check_format=check_format,
            info_dict=info_dict,
        )
        return selector.apply(formats, ctx)
    return func


# NB: Only these should be considered public functions
__all__ = [
    'build_format_selector',
    'format_resolution',
    'FormatType',
    'decompose_formats',
    'merge_formats',
]
