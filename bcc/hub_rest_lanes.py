"""Dedicated REST history capacity; all admissions still use one PublicCache.

This module never obtains prices itself, opens sockets, or changes a TTL.
A history request cannot occupy the metadata executor. Both lanes are bounded
by the original total concurrency limit and keep the registry's fencing tokens.
"""
import logging

KLINE_URL = 'https://api.bybit.com/v5/market/kline'


def admit_rest_lanes(hub):
    """One nonblocking scheduling turn, after completed acknowledgements."""
    history_limit = hub.max_workers // 2
    other_limit = hub.max_workers - history_limit
    for history, capacity, pool in ((True, history_limit, hub._history_pool),
                                    (False, other_limit, hub._pool)):
        running = [item for item in hub._inflight.values()
                   if (item[1] == KLINE_URL) == history]
        free = capacity - len(running)
        if free <= 0:
            continue
        # Ask SQL for both classes even when only one worker is free. An
        # arbitrarily long priority queue cannot hide unserved cold symbols.
        available = hub.registry.due(
            limit=max(2, free), reserve_regular=1,
            exclude=tuple(hub._inflight), include_priority=True,
            include_generation=True, history_lane=history,
            skip_ws_routes=hub._websocket_queue_exclusions(),
        )
        regular_running = any(not item[2] for item in running)
        if not regular_running and any(not item[3] for item in available):
            # One regular admission per lane when demand exists. Preserve
            # remaining positions-first ordering for all other free slots.
            index = next(i for i, item in enumerate(available) if not item[3])
            available.insert(0, available.pop(index))
        for key, url, params, urgent, generation in available[:free]:
            if (url == KLINE_URL) != history:
                raise RuntimeError('REST lane returned wrong route')
            if key in hub._inflight:
                # A duplicate selection is not another transport authority.
                # Keep the original future and generation, never stop the
                # independent WS publisher or submit another GET for it.
                logging.warning('Duplicate REST demand skipped; original in-flight request retained')
                continue

            def fetch(request_url=url, request_params=params, is_urgent=urgent):
                try:
                    hub._fetch_public(request_url, request_params, urgent=is_urgent)
                    return None
                except Exception as exc:
                    return exc

            hub._inflight[key] = (pool.submit(fetch), url, urgent,
                                  hub._leader_token, generation, params)
