# Fixture custom training code for the endpoint audit.
# Mirrors the contract in ray_wrapper.py: no-arg constructor, and
# train(data_path, epochs, **hyperparameters) returning a fitted estimator.
import pandas as pd
from sklearn.tree import DecisionTreeClassifier


class Trainer:
    def train(self, data_path: str, epochs: int = 10, **kwargs):
        df = pd.read_csv(data_path)
        target = next((c for c in ("label", "target") if c in df.columns), df.columns[-1])
        y = df[target]
        X = df.drop(columns=[target])
        model = DecisionTreeClassifier(
            max_depth=max(2, int(kwargs.get("max_depth", 4) or 4)),
            random_state=42,
        )
        model.fit(X, y)
        return model
