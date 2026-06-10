import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM

REGIME_ORDER = ["crash", "bear", "neutral", "bull", "bull_run"]


class RegimeClassifier:
    def __init__(self, n_states: int = 5):
        self.n_states = n_states
        self.model: GaussianHMM | None = None
        self.state_to_regime: dict[int, str] = {}

    @staticmethod
    def _features(df: pd.DataFrame) -> np.ndarray:
        returns = df["Close"].pct_change()
        vol = returns.rolling(20).std()
        feat = pd.concat([returns, vol], axis=1).dropna()
        return feat.values

    def fit(self, df: pd.DataFrame) -> None:
        X = self._features(df)
        self.model = GaussianHMM(
            n_components=self.n_states,
            covariance_type="full",
            n_iter=200,
            random_state=42,
        )
        self.model.fit(X)
        # Rank hidden states by mean return → lowest = crash, highest = bull_run
        means = self.model.means_[:, 0]
        order = np.argsort(means)
        self.state_to_regime = {int(s): REGIME_ORDER[i] for i, s in enumerate(order)}

    def predict(self, df: pd.DataFrame) -> str:
        if self.model is None:
            raise RuntimeError("Model not fitted")
        X = self._features(df)
        states = self.model.predict(X)
        return self.state_to_regime[int(states[-1])]
