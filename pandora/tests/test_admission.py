"""Policy tests. No Incus, no worker, no clock."""
import unittest

from pandora.engine.admission import (CLASSES, Admission, AdmissionError, Store,
                                      ceiling_for, classify, percentile, reserve)


class Percentile(unittest.TestCase):
    def test_one_sample_is_its_own_p95(self):
        self.assertEqual(percentile([700], 95), 700)

    def test_nearest_rank_takes_the_top_of_a_small_sample(self):
        self.assertEqual(percentile([100, 200, 300, 400], 95), 400)

    def test_ignores_input_order(self):
        self.assertEqual(percentile([5, 1, 9, 3], 50), percentile([9, 3, 1, 5], 50))

    def test_no_samples_is_an_error(self):
        with self.assertRaises(AdmissionError):
            percentile([], 95)


class Reserve(unittest.TestCase):
    def test_cold_start_reserves_the_class_ceiling(self):
        self.assertEqual(reserve([], size_class='medium'), CLASSES['medium'])

    def test_two_samples_are_still_cold(self):
        self.assertEqual(reserve([600, 640], size_class='medium'), CLASSES['medium'])

    def test_three_samples_learn(self):
        self.assertEqual(reserve([600, 640, 620], size_class='medium'), 800)

    def test_margin_applies_to_p95_not_to_the_mean(self):
        # p95 of these is 2000, not the 1100 average.
        self.assertEqual(reserve([800, 900, 1000, 2000], size_class='large'), 2500)

    def test_floor_holds_a_tiny_job_off_the_bottom(self):
        self.assertEqual(reserve([10, 12, 11], size_class='small'), 512)

    def test_ceiling_caps_a_greedy_history(self):
        self.assertEqual(reserve([4000, 4000, 4000], size_class='medium'), CLASSES['medium'])

    def test_zero_and_negative_peaks_are_not_samples(self):
        self.assertEqual(reserve([0, -1, 600], size_class='medium'), CLASSES['medium'])

    def test_unknown_class_is_an_error(self):
        with self.assertRaises(AdmissionError):
            reserve([600, 600, 600], size_class='enormous')

    def test_floor_above_ceiling_is_an_error(self):
        with self.assertRaises(AdmissionError):
            reserve([600, 600, 600], size_class='small', floor=9999)

    def test_margin_below_one_is_an_error(self):
        with self.assertRaises(AdmissionError):
            reserve([600, 600, 600], size_class='medium', margin=0.5)


class Classify(unittest.TestCase):
    def test_no_history_keeps_the_current_class(self):
        self.assertEqual(classify([], current='large'), 'large')

    def test_never_shrinks_below_the_operator_class(self):
        self.assertEqual(classify([100, 120], current='large'), 'large')

    def test_grows_to_fit_an_observed_peak(self):
        self.assertEqual(classify([5000], current='small'), 'large')

    def test_beyond_every_class_takes_the_largest(self):
        self.assertEqual(classify([99999], current='small'), 'xlarge')


class StoreHistory(unittest.TestCase):
    def setUp(self):
        self.store = Store()

    def test_peaks_come_back_newest_first(self):
        for peak in (100, 200, 300):
            self.store.record('eichler', 'journey', peak, 'ok')
        self.assertEqual(self.store.peaks('eichler', 'journey'), [300, 200, 100])

    def test_repos_and_jobs_do_not_share_history(self):
        self.store.record('eichler', 'journey', 100, 'ok')
        self.assertEqual(self.store.peaks('eichler', 'typecheck'), [])
        self.assertEqual(self.store.peaks('other', 'journey'), [])

    def test_failed_runs_still_teach_memory(self):
        self.store.record('eichler', 'journey', 900, 'failed')
        self.assertEqual(self.store.peaks('eichler', 'journey'), [900])

    def test_oom_peaks_are_stored_but_never_learned_from(self):
        self.store.record('eichler', 'journey', 4096, 'oom')
        self.assertEqual(self.store.peaks('eichler', 'journey'), [])

    def test_unknown_outcome_is_an_error(self):
        with self.assertRaises(AdmissionError):
            self.store.record('eichler', 'journey', 100, 'exploded')

    def test_non_integer_peak_is_an_error(self):
        with self.assertRaises(AdmissionError):
            self.store.record('eichler', 'journey', 100.5, 'ok')

    def test_class_is_per_job_and_defaults(self):
        self.assertEqual(self.store.size_class('eichler', 'journey'), 'medium')
        self.store.set_class('eichler', 'journey', 'large')
        self.assertEqual(self.store.size_class('eichler', 'journey'), 'large')
        self.assertEqual(self.store.size_class('eichler', 'typecheck'), 'medium')


class Admit(unittest.TestCase):
    def setUp(self):
        self.admission = Admission(budget_mib=12288)

    def learn(self, peaks, repo='eichler', job='journey'):
        for peak in peaks:
            self.admission.store.record(repo, job, peak, 'ok')

    def test_cold_first_run_reserves_the_ceiling(self):
        decision = self.admission.admit('r1', 'eichler', 'journey')
        self.assertTrue(decision['admitted'])
        self.assertTrue(decision['cold_start'])
        self.assertEqual(decision['reservation_mib'], CLASSES['medium'])

    def test_learned_reservation_admits_more_runs_than_cold(self):
        self.learn([2900, 2950, 2880])
        reservations = []
        for index in range(4):
            decision = self.admission.admit('r%d' % index, 'eichler', 'journey')
            if not decision['admitted']:
                break
            reservations.append(decision['reservation_mib'])
        self.assertEqual(reservations, [3688] * 3)          # 2950 * 1.25
        self.assertEqual(len(self.admission.running), 3)    # 4th does not fit 12288

    def test_refusal_names_memory_and_shows_the_arithmetic(self):
        self.learn([2900, 2950, 2880])
        for index in range(3):
            self.admission.admit('r%d' % index, 'eichler', 'journey')
        decision = self.admission.admit('r9', 'eichler', 'journey')
        self.assertEqual(decision['reason'], 'memory')
        self.assertEqual(decision['held_mib'], 3 * 3688)
        self.assertGreater(decision['held_mib'] + decision['reservation_mib'], 12288)

    def test_slot_limit_refuses_before_memory_does(self):
        admission = Admission(budget_mib=100000, max_running=2)
        admission.store.record('eichler', 'journey', 100, 'ok')
        admission.store.record('eichler', 'journey', 100, 'ok')
        admission.store.record('eichler', 'journey', 100, 'ok')
        admission.admit('a', 'eichler', 'journey')
        admission.admit('b', 'eichler', 'journey')
        self.assertEqual(admission.admit('c', 'eichler', 'journey')['reason'], 'slots')

    def test_finishing_releases_the_reservation(self):
        self.learn([2900, 2950, 2880])
        self.admission.admit('r1', 'eichler', 'journey')
        self.assertEqual(self.admission.held(), 3688)
        self.admission.finish('r1', 2900, 'ok')
        self.assertEqual(self.admission.held(), 0)

    def test_double_admission_of_one_run_is_an_error(self):
        self.admission.admit('r1', 'eichler', 'journey')
        with self.assertRaises(AdmissionError):
            self.admission.admit('r1', 'eichler', 'journey')

    def test_finishing_an_unadmitted_run_is_an_error(self):
        with self.assertRaises(AdmissionError):
            self.admission.finish('ghost', 100, 'ok')

    def test_over_reservation_under_ceiling_is_allowed_and_raises_the_next(self):
        self.learn([600, 620, 610])
        decision = self.admission.admit('r1', 'eichler', 'journey')
        self.assertEqual(decision['reservation_mib'], 775)
        outcome = self.admission.finish('r1', 1600, 'ok')
        self.assertTrue(outcome['over_reservation'])
        self.assertFalse(outcome['over_ceiling'])
        self.assertEqual(outcome['next_reservation_mib'], 2000)   # 1600 * 1.25

    def test_over_ceiling_is_oom_and_does_not_teach(self):
        self.learn([600, 620, 610])
        self.admission.admit('r1', 'eichler', 'journey')
        outcome = self.admission.finish('r1', CLASSES['medium'], 'oom')
        self.assertTrue(outcome['over_ceiling'])
        self.assertFalse(outcome['over_reservation'])
        self.assertEqual(outcome['next_reservation_mib'], 775)    # unchanged

    def test_repeated_ooms_never_escalate_the_reservation(self):
        self.learn([600, 620, 610])
        for index in range(5):
            self.admission.admit('r%d' % index, 'eichler', 'journey')
            self.admission.finish('r%d' % index, CLASSES['medium'], 'oom')
        self.assertEqual(self.admission.reservation('eichler', 'journey')[0], 775)

    def test_a_larger_class_raises_both_numbers(self):
        self.learn([4500, 4600, 4550])
        self.admission.store.set_class('eichler', 'journey', 'large')
        decision = self.admission.admit('r1', 'eichler', 'journey')
        self.assertEqual(decision['ceiling_mib'], CLASSES['large'])
        self.assertEqual(decision['reservation_mib'], 5750)

    def test_budget_must_be_a_sane_integer(self):
        with self.assertRaises(AdmissionError):
            Admission(budget_mib=1)

    def test_snapshot_is_stable_for_evidence(self):
        self.learn([600, 620, 610])
        self.admission.admit('r1', 'eichler', 'journey')
        self.assertEqual(self.admission.snapshot(),
                         {'budget_mib': 12288, 'held_mib': 775,
                          'running': {'r1': {'repo': 'eichler', 'job': 'journey',
                                             'reservation': 775, 'ceiling': 4096}}})


class Ceilings(unittest.TestCase):
    def test_classes_are_ordered_and_positive(self):
        values = [CLASSES[name] for name in ('small', 'medium', 'large', 'xlarge')]
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(value > 0 for value in values))

    def test_ceiling_for_rejects_an_unknown_class(self):
        with self.assertRaises(AdmissionError):
            ceiling_for('gigantic')


if __name__ == '__main__':
    unittest.main()
