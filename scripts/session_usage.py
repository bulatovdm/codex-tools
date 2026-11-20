"""Summarize local Codex token metadata without sending any requests."""

import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime


TOKEN_FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens')
# USD per million tokens, Standard with a 30-minute cache write, checked 2026-11-18.
# https://developers.openai.com/api/docs/pricing
# https://developers.openai.com/api/docs/models/gpt-6-sol
STANDARD_RATES = {
    'gpt-6.2-astra': (8, .40, 10, 40),
    'gpt-6.2-sol': (2, .10, 2.50, 10),
    'gpt-6.1-sol': (2, .10, 2.50, 10),
    'gpt-6.1-terra': (1, .05, 1.25, 5),
    'gpt-6-astra': (10, 1, 12.50, 50),
    'gpt-6-sol': (2, .20, 2.50, 10),
    'gpt-6-luna': (.10, .01, .125, .50),
}
CACHE_VERSION = 3
MAX_LINE_BYTES = 1024 * 1024
METADATA = re.compile(rb'"(?:session_meta|turn_context|token_count|thread_settings_applied|task_started|task_complete|turn_aborted|task_failed)"')
ZERO = (0, 0, 0, 0)
TIERS = {'default': 'Standard', 'priority': 'Fast', 'ultrafast': 'Ultrafast'}


def read_counts(value):
    if not isinstance(value, dict) or not {'input_tokens', 'output_tokens'} <= value.keys():
        return None
    counts = tuple(value.get(field, 0) for field in TOKEN_FIELDS)
    return counts if all(type(count) is int and count >= 0 for count in counts) else None


def add_counts(left, right):
    return [a + b for a, b in zip(left, right)]


def event_time(event):
    try:
        return datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp()
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def record_pace_boundary(state, event, payload):
    kind = payload.get('type')
    turn = state['turn']
    if kind == 'thread_settings_applied':
        settings = payload.get('thread_settings')
        tier = settings.get('service_tier') if isinstance(settings, dict) else None
        if isinstance(tier, str) and tier != state['tier']:
            state['tier'] = tier
            if turn and turn['completed_at'] is None:
                # A request already in flight can finish after a tier switch.
                # Its first new count establishes a baseline for this tier.
                turn.update(since=None, output_tokens=0)
            else:
                state['turn'] = None
    elif kind == 'task_started':
        at = event_time(event)
        turn_id = payload.get('turn_id')
        state['turn'] = (
            {'id': turn_id, 'started_at': at, 'since': at, 'output_tokens': 0, 'completed_at': None}
            if at is not None and isinstance(turn_id, str) else None
        )
    elif turn and turn['id'] == payload.get('turn_id'):
        if kind == 'task_complete':
            turn['completed_at'] = event_time(event)
            if turn['completed_at'] is None:
                state['turn'] = None
        elif kind in ('turn_aborted', 'task_failed'):
            state['turn'] = None


def record_usage(state, raw):
    if not METADATA.search(raw[:256]):
        return
    try:
        event = json.loads(raw)
    except ValueError:
        return
    if not isinstance(event, dict) or not isinstance(event.get('payload'), dict):
        return
    payload = event['payload']
    if event.get('type') == 'session_meta':
        state['openai'] = payload.get('model_provider') == 'openai'
        if payload.get('forked_from_id'):
            state['forked_at'] = payload.get('timestamp') or event.get('timestamp')
        return
    if event.get('type') == 'turn_context':
        model = payload.get('model')
        state['model'] = model if isinstance(model, str) and len(model) <= 128 else ''
        return
    if event.get('type') != 'event_msg':
        return
    if payload.get('type') != 'token_count':
        record_pace_boundary(state, event, payload)
        return
    info = payload.get('info')
    if not isinstance(info, dict):
        return
    total = read_counts(info.get('total_token_usage'))
    if total is None:
        return

    previous = state['previous']
    state['previous'] = total
    timestamp = event.get('timestamp')
    forked_at = state['forked_at']
    if isinstance(forked_at, str) and isinstance(timestamp, str) and timestamp < forked_at:
        return
    last = read_counts(info.get('last_token_usage'))
    delta = tuple(a - b for a, b in zip(total, previous or ZERO))
    if any(count < 0 for count in delta) or (forked_at and previous is None):
        # A fork can start with its parent's cumulative counters. The last
        # request is local to the fork, whereas the inherited baseline is not.
        delta = last if last is not None else (ZERO if forked_at and previous is None else total)
    if not any(delta):
        return
    state['counts'] = add_counts(state['counts'], delta)
    turn = state['turn']
    if turn and turn['completed_at'] is None and last == delta:
        if turn['since'] is None:
            turn['since'] = event_time(event)
        else:
            turn['output_tokens'] += delta[3]
    if state['openai'] and last == delta and delta[1] + delta[2] <= delta[0]:
        length = 'long' if delta[0] > 272_000 else 'short'
        bucket = state['model'] + ':' + length
        state['priced'][bucket] = add_counts(state['priced'].get(bucket, ZERO), delta)


def read_checkpoint(cache):
    try:
        if cache.stat().st_size <= 2_000_000:
            state = json.loads(cache.read_text())
            if (isinstance(state, dict) and state.get('version') == CACHE_VERSION
                    and type(state.get('offset')) is int and type(state.get('size')) is int
                    and isinstance(state.get('counts'), list) and len(state['counts']) == 4
                    and isinstance(state.get('priced'), dict)):
                return state
    except (OSError, ValueError):
        pass
    return {}


def save_checkpoint(cache, state):
    temporary = None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(mode='w', dir=cache.parent, delete=False) as output:
            temporary = output.name
            json.dump(state, output, separators=(',', ':'))
        os.replace(temporary, cache)
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def checkpoint_digest(source, offset):
    length = min(128, offset)
    source.seek(offset - length)
    return hashlib.blake2s(source.read(length)).hexdigest()


def scan_rollout(path, cache, deadline):
    """Scan complete lines once. Leave an in-flight line for the next refresh."""
    state = read_checkpoint(cache)
    with path.open('rb') as source:
        stat = os.fstat(source.fileno())
        identity = [stat.st_dev, stat.st_ino]
        offset = state.get('offset', 0)
        if (
            state.get('identity') != identity
            or not 0 <= offset <= stat.st_size
            or (stat.st_size <= state.get('size', 0) and state.get('mtime') != stat.st_mtime_ns)
            or (offset and state.get('digest') != checkpoint_digest(source, offset))
        ):
            state = {
                'version': CACHE_VERSION, 'identity': identity, 'offset': 0,
                'previous': None, 'counts': ZERO, 'priced': {}, 'openai': False,
                'model': '', 'forked_at': None, 'skip_line': False, 'tier': None, 'turn': None,
            }
        start = offset = state['offset']
        source.seek(offset)
        pending = False
        while offset < stat.st_size:
            if deadline is not None and time.monotonic() >= deadline:
                pending = True
                break
            raw = source.readline(min(MAX_LINE_BYTES, stat.st_size - offset))
            if raw.endswith(b'\n'):
                if not state['skip_line']:
                    record_usage(state, raw)
                state['skip_line'] = False
            elif len(raw) == MAX_LINE_BYTES:
                # Some tool records contain embedded images or large outputs.
                state['skip_line'] = True
            else:
                break
            offset += len(raw)
        if offset != start:
            state.update(offset=offset, size=stat.st_size, mtime=stat.st_mtime_ns,
                         digest=checkpoint_digest(source, offset))
            save_checkpoint(cache, state)
    return state, pending


def estimate_usd(bucket, counts):
    model, length = bucket.rsplit(':', 1)
    rates = STANDARD_RATES.get(model)
    if rates is None:
        return None
    if length == 'long':
        rates = (rates[0] * 2, rates[1] * 2, rates[2] * 2, rates[3] * 1.5)
    input_tokens, cached, written, output_tokens = counts
    billable = (input_tokens - cached - written, cached, written, output_tokens)
    return sum(count * rate for count, rate in zip(billable, rates)) / 1_000_000


def turn_pace(data, live_since, now):
    tier = TIERS.get(data['tier'])
    turn = data['turn']
    if turn is None:
        return {'tier': tier, 'state': 'idle', 'output_tokens_per_second': None}
    end = turn['completed_at']
    if end is None and (live_since is None or turn['started_at'] < live_since):
        return {'tier': tier, 'state': 'idle', 'output_tokens_per_second': None}
    seconds = (end if end is not None else now) - turn['since'] if turn['since'] is not None else 0
    speed = turn['output_tokens'] / seconds if seconds > 0 and turn['output_tokens'] else None
    return {
        'tier': tier, 'state': 'completed' if end is not None else 'running',
        'output_tokens_per_second': speed,
    }


def session_token_usage(members, directory, home, budget=None, *, pace_thread=None, live_since=None):
    """Aggregate the main thread and descendants, each rollout counted once."""
    scope = hashlib.sha256(os.fsencode(directory.resolve())).hexdigest()[:24]
    cache_dir = home / '.cache/codex-tools/session-usage' / scope
    deadline = time.monotonic() + budget if budget is not None else None
    counts = ZERO
    usd = 0.0
    priced_tokens = 0
    missing_rollouts = 0
    pace = None
    for native, rollout, edge in members:
        if not isinstance(rollout, str) or not rollout or not re.fullmatch(r'[a-f0-9-]{36}', native):
            missing_rollouts += 1
            continue
        try:
            data, pending = scan_rollout(directory / rollout, cache_dir / (native + '.json'), deadline)
        except OSError:
            missing_rollouts += 1
            continue
        if pending:
            return {'pending': True}
        counts = add_counts(counts, data['counts'])
        if native == pace_thread:
            pace = turn_pace(data, live_since if edge != 'closed' else None, time.time())
        for bucket, tokens in data['priced'].items():
            cost = estimate_usd(bucket, tokens)
            if cost is not None:
                usd += cost
                priced_tokens += tokens[0] + tokens[3]
    if missing_rollouts == len(members):
        return None
    total = counts[0] + counts[3]
    return {
        **dict(zip(TOKEN_FIELDS, counts)),
        'total_tokens': total,
        'api_standard_usd': round(usd, 8) if priced_tokens else None,
        'api_priced_tokens': priced_tokens,
        'missing_rollouts': missing_rollouts,
        'turn_pace': pace,
    }
