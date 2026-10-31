import math
import random

from genetic_lqo.search.hint_search import scheduler as s


def lognormal_truncated_mean(mu, sigma, cutoff, n=200_000):
    rng = random.Random(0)
    return sum(min(math.exp(rng.gauss(mu, sigma)), cutoff) for _ in range(n)) / n


def test_probability_and_expected_time_match_their_definitions():
    mu, sigma, best, cutoff = math.log(0.5), 0.8, 0.4, 0.8
    assert abs(s.improvement_probability(mu, sigma, best) - s.norm_cdf((math.log(best) - mu) / sigma)) < 1e-12
    assert abs(s.expected_time(mu, sigma, cutoff) - lognormal_truncated_mean(mu, sigma, cutoff)) < 5e-3
    assert s.expected_time(10.0, 0.5, cutoff) == cutoff


def test_censored_observations_lower_a_task_rate():
    sigma, best, cutoff = 0.5, 1.0, 2.0
    rng = random.Random(1)
    measured = s.Posterior(1.0, [(0.9, False), (1.1, False)], sigma, sigma)
    censored = s.Posterior(1.0, [(2.0, True), (2.0, True)], sigma, sigma)
    mean = lambda p: sum(p.sample(rng) for _ in range(2000)) / 2000
    assert mean(censored) > mean(measured) + 0.3
    assert s.improvement_rate(mean(measured), sigma, best, cutoff) > s.improvement_rate(mean(censored), sigma, best,
                                                                                          cutoff)


def test_sigma_mle_uses_censoring_and_defaults_without_data():
    assert s.estimate_sigma([[(1.0, False)]]) == s.DEFAULT_SIGMA
    rng = random.Random(2)
    group = [(math.exp(rng.gauss(0, 0.7)), False) for _ in range(400)]
    assert abs(s.estimate_sigma([group]) - 0.7) < 0.08
    cut = math.exp(0.5)
    censored = [(min(x, cut), x >= cut) for x, _ in group]
    naive = [(x, False) for x, c in censored if not c]
    assert abs(s.estimate_sigma([censored]) - 0.7) < abs(s.estimate_sigma([naive]) - 0.7)


def test_every_eligible_task_has_positive_selection_probability():
    good = s.Posterior(0.5, [(0.5, False)] * 4, 0.5, 0.5)
    bad = s.Posterior(1.0, [(1.2, False), (2.0, True)], 0.5, 0.5)
    rng = random.Random(4)
    picks = [s.select([("good", good, 0.5), ("bad", bad, 0.5)], 1.0, 2.0, rng)[0] for _ in range(3000)]
    assert picks.count("good") > picks.count("bad") > 0
