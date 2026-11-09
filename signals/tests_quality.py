"""Testes das verificações de qualidade (signals/quality.py) com sinais sintéticos.

Usam unittest puro: rodam com `python manage.py test signals` ou `python -m unittest`.
"""
import unittest

import numpy as np

from signals.quality import check_recording, identical_channels, sample_slips, saturation

FS = 250.0


def _eeg(seconds=40, ch=8, seed=0, mains_uv=5.0):
    rng = np.random.default_rng(seed)
    n = int(seconds * FS)
    t = np.arange(n) / FS
    x = rng.normal(0, 20, (n, ch))                      # "EEG" de ~20 µV
    x += mains_uv * np.sin(2 * np.pi * 60.0 * t)[:, None]
    return x


class CleanRecordingTest(unittest.TestCase):
    def test_clean_signal_passes(self):
        rep = check_recording(_eeg(), FS)
        self.assertTrue(rep.ok, rep.flags)
        self.assertEqual(rep.identical_pairs, [])
        self.assertEqual(rep.dead_channels, [])


class MainsTest(unittest.TestCase):
    def test_strong_mains_is_flagged(self):
        rep = check_recording(_eeg(mains_uv=50_000), FS)
        self.assertTrue(any("rede" in f for f in rep.flags))
        self.assertGreater(min(rep.mains_share), 0.9)


class SaturationTest(unittest.TestCase):
    def test_dead_channel(self):
        x = _eeg()
        x[:, 5] = 187_500.0
        frac, dead = saturation(x)
        self.assertEqual(dead, [6])
        self.assertAlmostEqual(frac[5], 1.0)


class IdenticalChannelsTest(unittest.TestCase):
    def test_copied_channel(self):
        x = _eeg()
        x[:, 4] = x[:, 0] + np.random.default_rng(1).normal(0, 2, len(x))
        self.assertEqual([(i, j) for i, j, _ in identical_channels(x)], [(1, 5)])

    def test_common_mains_is_not_identity(self):
        # rede comum de 30 mV dá r > 0,999, mas os canais diferem pelo EEG (~28 µV de DP)
        x = _eeg(mains_uv=30_000)
        self.assertEqual(identical_channels(x), [])


class SlipTest(unittest.TestCase):
    def test_dropped_and_duplicated_samples(self):
        x = _eeg(mains_uv=30_000)[:, 0]
        i4, i8 = int(4 * FS), int(8 * FS)
        x = np.concatenate([x[:i4], x[i4 + 1:i8], x[i8 - 1:i8], x[i8:]])  # perde 1 amostra em 4 s, duplica em ~8 s
        found = sample_slips(x, FS)
        self.assertEqual(len(found), 2, found)
        self.assertAlmostEqual(found[0], 4.0, delta=0.1)
        self.assertAlmostEqual(found[1], 8.0, delta=0.1)

    def test_no_false_slips(self):
        self.assertEqual(sample_slips(_eeg(mains_uv=30_000)[:, 0], FS), [])


class EdgeTransientTest(unittest.TestCase):
    def test_slow_drift_produces_edge_flag(self):
        x = _eeg()
        tt = np.arange(len(x)) / FS
        x += 50_000 * np.exp(-tt / 0.3)[:, None] * np.linspace(0.5, 1.5, 8)  # acomodação do eletrodo no início
        rep = check_recording(x, FS)
        self.assertTrue(any("borda" in f for f in rep.flags), rep.flags)


class EmptyRecordingTest(unittest.TestCase):
    def test_all_nan(self):
        rep = check_recording(np.full((2500, 8), np.nan), FS)
        self.assertFalse(rep.ok)
        self.assertIn("sem dados", rep.flags[0])


if __name__ == "__main__":
    unittest.main()


class CompareRecordingsTest(unittest.TestCase):
    def test_compare_two_csvs(self):
        import os
        import tempfile

        import pandas as pd

        from signals.quality import compare_recordings

        def write(path, x):
            df = pd.DataFrame(x, columns=[f"EEG Channel {i}" for i in range(1, 9)])
            df.insert(0, "Timestamp", np.arange(len(x)) * 4)
            df["Marker tag"] = ""
            df.loc[0, "Marker tag"] = "[Marker]"
            df.to_csv(path, index=False)

        with tempfile.TemporaryDirectory() as d:
            good, bad = _eeg(seconds=12), _eeg(seconds=12, seed=2)
            bad[:, 4] = bad[:, 0]
            write(os.path.join(d, "a.csv"), good)
            write(os.path.join(d, "b.csv"), bad)
            rows = compare_recordings([os.path.join(d, "a.csv"), os.path.join(d, "b.csv")], ["P01", "P02"])
        self.assertTrue(rows[0]["ok"], rows[0]["problemas"])
        self.assertFalse(rows[1]["ok"])
        self.assertEqual(rows[1]["canais_identicos"], "1 5")
