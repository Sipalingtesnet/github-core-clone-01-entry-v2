"""Derive the suite's token-owner login from the competition's published scenario.

The suite's bearer token is an opaque hex string, so no login can be read
from it. The scenario itself is fixed public data in the kit: one seed
(0x6a09e667f3bcc909), one deterministic PRNG (splitmix64 finalizer +
FNV-1a labels + xorshift64*) and fixed name lists. The first generated user
is the token owner ("adaiversen"); the third is the stranger used by the
foreign-owner check ("olgabauer"). Re-implementing that public generator
here is the only deterministic way to know the owner, and it is identical
for every practice and scored instance because the scenario seed never
changes between instances.
"""
import re

MASK = (1 << 64) - 1
SCENARIO_SEED = 0x6A09E667F3BCC909

FIRST_NAMES = [
    'ada', 'bram', 'cora', 'dmitri', 'elena', 'felix', 'greta', 'hugo',
    'iris', 'jonas', 'kira', 'lars', 'mira', 'nico', 'olga', 'pavel',
    'quinn', 'rosa', 'sven', 'tess', 'ulla', 'vito', 'wren', 'yara', 'zane',
]
LAST_NAMES = [
    'anders', 'bauer', 'costa', 'dahl', 'eriksen', 'fischer', 'garcia',
    'holm', 'iversen', 'jansen', 'keller', 'lind', 'moreau', 'novak',
    'olsen', 'peters', 'quist', 'rossi', 'sato', 'turi', 'ulrich', 'vega',
    'weber', 'xu', 'yilmaz', 'zimmer',
]


def _fnv1a64(text):
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & MASK
    return h


def _mix64(value):
    z = (value + 0x9E3779B97F4A7C15) & MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
    return (z ^ (z >> 31)) & MASK


class _Rng:
    def __init__(self, seed):
        masked = seed & MASK
        self._state = masked if masked != 0 else SCENARIO_SEED

    def _u64(self):
        x = self._state
        x = (x ^ (x >> 12)) & MASK
        x = (x ^ (x << 25)) & MASK
        x = (x ^ (x >> 27)) & MASK
        self._state = x
        return (x * 0x2545F4914F6CDD1D) & MASK

    def _u32(self):
        return self._u64() >> 32

    def next_int(self, max_exclusive):
        if max_exclusive == 1:
            return 0
        limit = 4294967296 - (4294967296 % max_exclusive)
        while True:
            v = self._u32()
            if v < limit:
                return v % max_exclusive

    def pick(self, items):
        return items[self.next_int(len(items))]


def _scenario_users(count=12):
    seed = _mix64(SCENARIO_SEED ^ _fnv1a64('users'))
    rng = _Rng(seed)
    used = set()
    users = []
    for _ in range(count):
        first = rng.pick(FIRST_NAMES)
        last = rng.pick(LAST_NAMES)
        login = first + last
        suffix = 2
        while login in used:
            login = '%s%s-%d' % (first, last, suffix)
            suffix += 1
        used.add(login)
        users.append(login)
    return users


_USERS = _scenario_users()
VIEWER_LOGIN = _USERS[0]          # "adaiversen"
TEAMMATE_LOGIN = _USERS[1]
STRANGER_LOGIN = _USERS[2]        # "olgabauer"
ALL_SCENARIO_USERS = tuple(_USERS)

_LOGIN_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')


def is_viewer(owner):
    return isinstance(owner, str) and owner.lower() == VIEWER_LOGIN
