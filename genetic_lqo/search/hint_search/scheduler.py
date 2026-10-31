from __future__ import annotations

import bisect
import math
import random
from collections.abc import Sequence

DEFAULT_SIGMA = 1.0
MIN_SIGMA = 0.05
_GRID_POINTS = 400
_SQRT2 = math.sqrt(2.0)

Observation = tuple[float, bool]


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / _SQRT2)


def norm_logsf(x: float) -> float:
    survival = 0.5 * math.erfc(x / _SQRT2)
    return math.log(survival) if survival > 0 else -0.5 * x * x - math.log(x * math.sqrt(2 * math.pi))


class Sample:

    def __init__(self, observations: Sequence[Observation]):
        logs = [math.log(seconds) for seconds, censored in observations if not censored]
        self.n, self.s1, self.s2 = len(logs), sum(logs), sum(y * y for y in logs)
        self.censored: dict[float, int] = {}
        for seconds, censored in observations:
            if censored:
                key = math.log(seconds)
                self.censored[key] = self.censored.get(key, 0) + 1
        values = logs + list(self.censored)
        self.low, self.high = (min(values), max(values)) if values else (0.0, 0.0)

    def __bool__(self) -> bool:
        return bool(self.n or self.censored)

    def log_likelihood(self, mu: float, sigma: float) -> float:
        total = -(self.s2 - 2 * mu * self.s1 + self.n * mu * mu) / (2 * sigma * sigma) - self.n * math.log(sigma)
        for log_u, count in self.censored.items():
            total += count * norm_logsf((log_u - mu) / sigma)
        return total


def log_likelihood(observations: Sequence[Observation], mu: float, sigma: float) -> float:
    return Sample(observations).log_likelihood(mu, sigma)


def improvement_probability(mu: float, sigma: float, best_seconds: float) -> float:
    return norm_cdf((math.log(best_seconds) - mu) / sigma)


def expected_time(mu: float, sigma: float, cutoff_seconds: float) -> float:
    log_u = math.log(cutoff_seconds)
    below = math.exp(mu + sigma * sigma / 2) * norm_cdf((log_u - mu - sigma * sigma) / sigma)
    return below + cutoff_seconds * (1 - norm_cdf((log_u - mu) / sigma))


def improvement_rate(mu: float, sigma: float, best_seconds: float, cutoff_seconds: float) -> float:
    return improvement_probability(mu, sigma, best_seconds) / expected_time(mu, sigma, cutoff_seconds)


def _golden_max(f, low: float, high: float, iterations: int = 40) -> float:
    ratio = (math.sqrt(5) - 1) / 2
    a, b = low, high
    c, d = b - ratio * (b - a), a + ratio * (b - a)
    fc, fd = f(c), f(d)
    for _ in range(iterations):
        if fc >= fd:
            b, d, fd = d, c, fc
            c = b - ratio * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + ratio * (b - a)
            fd = f(d)
    return (a + b) / 2


def task_mle(sample: Sample, sigma: float) -> float:
    if not sample.censored:
        return sample.s1 / sample.n
    return _golden_max(lambda mu: sample.log_likelihood(mu, sigma), sample.low - 5 * sigma, sample.high + 5 * sigma)


def estimate_sigma(groups: Sequence[Sequence[Observation]]) -> float:
    samples = [Sample(group) for group in groups if group]
    if sum(sample.n for sample in samples) < 2:
        return DEFAULT_SIGMA

    def profile(log_sigma: float) -> float:
        sigma = math.exp(log_sigma)
        return sum(sample.log_likelihood(task_mle(sample, sigma), sigma) for sample in samples)

    return max(MIN_SIGMA, math.exp(_golden_max(profile, math.log(MIN_SIGMA), math.log(20.0), 30)))


def estimate_prior_scale(tasks: Sequence[tuple[float, Sequence[Observation]]], sigma: float) -> float:
    observed = [(math.log(base), Sample(group)) for base, group in tasks if group]
    if len(observed) < 2:
        return sigma
    drift = [(task_mle(sample, sigma) - center) ** 2 for center, sample in observed]
    return max(MIN_SIGMA, math.sqrt(sum(drift) / len(drift)))


class Posterior:

    def __init__(self, base_seconds: float, observations: Sequence[Observation], sigma: float, prior_scale: float):
        center = math.log(base_seconds)
        sample = Sample(observations)
        low, high = (sample.low, sample.high) if sample else (center, center)
        low = min(center - 5 * prior_scale, low - 5 * sigma)
        high = max(center + 5 * prior_scale, high + 5 * sigma)
        self.step = (high - low) / (_GRID_POINTS - 1)
        self.grid = [low + i * self.step for i in range(_GRID_POINTS)]
        log_post = [-0.5 * ((mu - center) / prior_scale) ** 2 + sample.log_likelihood(mu, sigma)
                    for mu in self.grid]
        peak = max(log_post)
        self.cumulative = []
        total = 0.0
        for value in log_post:
            total += math.exp(value - peak)
            self.cumulative.append(total)

    def sample(self, rng: random.Random) -> float:
        index = bisect.bisect_left(self.cumulative, rng.random() * self.cumulative[-1])
        return self.grid[min(index, len(self.grid) - 1)] + (rng.random() - 0.5) * self.step


def select(candidates: Sequence[tuple[object, Posterior, float]], best_seconds: float, cutoff_seconds: float,
           rng: random.Random) -> tuple[object, list[dict]]:
    scores = []
    for task, posterior, sigma in candidates:
        mu = posterior.sample(rng)
        scores.append((improvement_rate(mu, sigma, best_seconds, cutoff_seconds), mu, task))
    _, _, chosen = max(scores, key=lambda item: item[0])
    return chosen, [{"task": getattr(task, "id", None), "mu": round(mu, 4), "rate": rate} for rate, mu, task in scores]
