"""
Benchmark for the project report (Section 5): Linear Search vs Hash Table lookup.

Task measured: given an office id, find that office's record — the same kind of
lookup the system does when it validates the offices an organization chose
(`_route_for`) and when it maps status codes to labels.

Run:  python tests/benchmark_search.py
"""
import random
import statistics
import time

SIZES = [10, 100, 1_000, 10_000, 100_000]
LOOKUPS = 1_000      # lookups per trial
TRIALS = 7           # repeat each measurement, report the median
random.seed(2026)


def build(n):
    records = [{"id": i, "name": f"Office {i}", "is_active": 1} for i in range(1, n + 1)]
    table = {r["id"]: r for r in records}            # hash table: id -> record
    return records, table


def linear_search(records, target):
    for r in records:                                 # O(n): check one by one
        if r["id"] == target:
            return r
    return None


def hash_lookup(table, target):
    return table.get(target)                          # O(1) average


def time_it(fn, container, targets):
    t0 = time.perf_counter()
    for t in targets:
        fn(container, t)
    return (time.perf_counter() - t0) / len(targets) * 1e6   # microseconds per lookup


def main():
    print(f"{'n':>8} | {'linear avg (us)':>15} | {'hash avg (us)':>13} | {'linear miss (us)':>16} | {'hash miss (us)':>14} | speed-up")
    for n in SIZES:
        records, table = build(n)
        lin, hsh, lin_miss, hsh_miss = [], [], [], []
        for _ in range(TRIALS):
            hits = [random.randint(1, n) for _ in range(LOOKUPS)]          # average case
            misses = [n + 1 + i for i in range(LOOKUPS)]                    # worst case: not found
            lin.append(time_it(linear_search, records, hits))
            hsh.append(time_it(hash_lookup, table, hits))
            lin_miss.append(time_it(linear_search, records, misses))
            hsh_miss.append(time_it(hash_lookup, table, misses))
        m = statistics.median
        # sanity: both methods return the same record
        for t in random.sample(range(1, n + 1), min(n, 50)):
            assert linear_search(records, t) is hash_lookup(table, t)
        print(f"{n:>8} | {m(lin):>15.3f} | {m(hsh):>13.3f} | {m(lin_miss):>16.3f} | {m(hsh_miss):>14.3f} | {m(lin) / m(hsh):>7.0f}x")


if __name__ == "__main__":
    main()
