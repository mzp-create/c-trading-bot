#!/usr/bin/env python3
"""
ML-based Market Predictor
Uses ensemble ML models to predict price direction.
"""

import os
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from pathlib import Path
import joblib

from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score, confusion_matrix
from sklearn.pipeline import Pipeline


class MLPredictor:
    """Machine learning model for price direction prediction.

    Supports multiple symbols by storing per-symbol models on disk.
    Each symbol gets its own model file: ``data/models/{base}_{quote}_ml_model.joblib``.
    """

    def __init__(self, config: dict):
        self.config = config
        self.log = logging.getLogger("MLPredictor")

        # Per-symbol state — lazily populated on first train/predict
        self._models: Dict[str, dict] = {}       # symbol -> {'rf': ..., 'gb': ...}
        self._scalers: Dict[str, StandardScaler] = {}
        self._feature_names: Dict[str, list] = {}
        self._last_train_times: Dict[str, datetime] = {}
        self._model_loaded: Dict[str, bool] = {}
        self._training_accuracies: Dict[str, float] = {}
        self._feature_importances: Dict[str, dict] = {}

        models_dir = Path(config.get('data', {}).get('models_dir', 'data/models'))
        self.models_dir = models_dir
        self._ensure_dirs()

        self.log.info("ML Predictor initialized (multi-symbol mode)")

    def _get_model_path(self, symbol: str) -> Path:
        """Return path for a symbol's model file, e.g. BTC_USDT_ml_model.joblib."""
        safe = symbol.replace('/', '_')
        return self.models_dir / f'{safe}_ml_model.joblib'

    def _get_scaler_path(self, symbol: str) -> Path:
        """Return path for a symbol's scaler file, e.g. BTC_USDT_scaler.joblib."""
        safe = symbol.replace('/', '_')
        return self.models_dir / f'{safe}_scaler.joblib'

    @property
    def model_loaded(self) -> bool:
        """Legacy property — returns True if *any* symbol has a loaded model."""
        return any(self._model_loaded.values()) if self._model_loaded else False

    @property
    def last_train_time(self) -> datetime:
        """Legacy property — returns the latest train time across all symbols."""
        if not self._last_train_times:
            return datetime.min
        return max(self._last_train_times.values())

    def _ensure_dirs(self):
        self.models_dir.mkdir(parents=True, exist_ok=True)

    def prepare_features(self, df: pd.DataFrame, symbol: str = "BTC/USDT") -> pd.DataFrame:
        """Compute feature set from OHLCV data."""
        if df is None or len(df) < 50:
            return pd.DataFrame()

        df = df.copy()
        features = pd.DataFrame(index=df.index)

        close = df['close'].values
        high = df['high'].values
        low = df['low'].values
        volume = df['volume'].values

        # --- Price-based features ---
        # Returns at different horizons
        for p in [1, 3, 6, 12, 24]:
            if len(close) > p:
                features[f'return_{p}h'] = pd.Series(
                    np.where(df['close'].shift(p).values != 0,
                             (close - df['close'].shift(p).values) / df['close'].shift(p).values,
                             0),
                    index=df.index
                )

        # Log returns
        log_ret = np.log(close / np.roll(close, 1))
        log_ret[0] = 0
        features['log_return'] = log_ret

        # --- Technical indicator features ---
        # RSI proxy
        delta = pd.Series(np.diff(close, prepend=close[0]))
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        features['rsi'] = 100 - (100 / (1 + rs))
        features['rsi'] = features['rsi'].fillna(50)

        # Price relative to rolling min/max
        for w in [10, 20, 50]:
            features[f'price_vs_high_{w}'] = (close / pd.Series(high).rolling(w).max().values) - 1
            features[f'price_vs_low_{w}'] = (close / pd.Series(low).rolling(w).min().values) - 1

        # --- Volume features ---
        vol_series = pd.Series(volume)
        features['volume_ratio'] = vol_series / vol_series.rolling(20).mean().replace(0, np.nan)
        features['volume_ratio'] = features['volume_ratio'].fillna(1)

        # Volume trend
        features['volume_trend'] = vol_series.rolling(5).mean() / vol_series.rolling(20).mean().replace(0, np.nan)
        features['volume_trend'] = features['volume_trend'].fillna(1)

        # --- Volatility features ---
        features['volatility_10'] = pd.Series(close).pct_change().rolling(10).std().values
        features['volatility_30'] = pd.Series(close).pct_change().rolling(30).std().values

        # --- Moving average features ---
        for w in [7, 14, 21, 50]:
            ma = pd.Series(close).rolling(w).mean().values
            features[f'ma_{w}_ratio'] = close / np.where(ma != 0, ma, close)

        # --- Time features ---
        if hasattr(df.index, 'hour'):  # DatetimeIndex
            features['hour'] = df.index.hour
            features['day_of_week'] = df.index.dayofweek
            # Cyclical encoding
            features['hour_sin'] = np.sin(2 * np.pi * features['hour'] / 24)
            features['hour_cos'] = np.cos(2 * np.pi * features['hour'] / 24)

        # --- Price acceleration ---
        price_changes = pd.Series(close).diff()
        features['acceleration'] = price_changes.diff().values

        # --- Spread features (high-low range) ---
        features['hl_range'] = (high - low) / close
        features['hl_range_ma'] = pd.Series(features['hl_range'].values).rolling(14).mean().values

        # Drop NaN rows from feature computation
        features = features.replace([np.inf, -np.inf], np.nan)
        features = features.dropna()

        self._feature_names[symbol] = features.columns.tolist()
        return features

    def prepare_target(self, df: pd.DataFrame, forward_periods: int = 3,
                       profit_threshold: float = 0.008) -> pd.Series:
        """
        Create binary target: 1 if price goes up by profit_threshold in forward_periods,
        0 if it goes down by profit_threshold, else NaN (skip).

        Default: predict if price will move 0.8% up in 3 periods.
        For 1h data: predicts 3h forward, targeting 0.8% moves.
        """
        if df is None or len(df) < forward_periods + 1:
            return pd.Series(dtype=float)

        future_close = df['close'].shift(-forward_periods)
        current_close = df['close']

        pct_change = (future_close - current_close) / current_close

        target = pd.Series(index=df.index, dtype=float)
        target.loc[pct_change >= profit_threshold] = 1.0
        target.loc[pct_change <= -profit_threshold] = 0.0
        # Drop uncertain periods
        target = target.dropna()

        return target

    def train(self, df: pd.DataFrame, symbol: str = "BTC/USDT") -> Dict:
        """Train the ensemble ML model for a given symbol."""
        if df is None or len(df) < 200:
            self.log.warning(f"[{symbol}] Insufficient data for training: {len(df) if df is not None else 0} rows")
            return {'status': 'failed', 'reason': 'insufficient_data', 'symbol': symbol}

        features = self.prepare_features(df, symbol)
        if features.empty:
            return {'status': 'failed', 'reason': 'no_features', 'symbol': symbol}

        target = self.prepare_target(df)
        if target.empty:
            return {'status': 'failed', 'reason': 'no_valid_targets', 'symbol': symbol}

        # Align features and target
        common_idx = features.index.intersection(target.index)
        X = features.loc[common_idx].values
        y = target.loc[common_idx].values

        if len(X) < 50:
            return {'status': 'failed', 'reason': f'too_few_samples:{len(X)}', 'symbol': symbol}

        # Check class balance
        n_pos = np.sum(y == 1)
        n_neg = np.sum(y == 0)
        self.log.info(f"[{symbol}] Training samples: {len(X)} total, {n_pos} up, {n_neg} down")

        if n_pos < 10 or n_neg < 10:
            return {'status': 'failed', 'reason': f'unbalanced:{n_pos}/{n_neg}', 'symbol': symbol}

        # Split
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42, stratify=y
        )

        # Scale features
        scaler = StandardScaler()
        X_train_scaled = scaler.fit_transform(X_train)
        X_test_scaled = scaler.transform(X_test)

        # Train ensemble: RandomForest + GradientBoosting
        rf = RandomForestClassifier(
            n_estimators=200,
            max_depth=10,
            min_samples_split=10,
            min_samples_leaf=5,
            class_weight='balanced',
            random_state=42,
            n_jobs=-1
        )

        gb = GradientBoostingClassifier(
            n_estimators=100,
            max_depth=4,
            min_samples_split=10,
            min_samples_leaf=5,
            learning_rate=0.05,
            subsample=0.8,
            random_state=42
        )

        # Train both
        rf.fit(X_train_scaled, y_train)
        gb.fit(X_train_scaled, y_train)

        # Ensemble: average probabilities
        rf_probs = rf.predict_proba(X_test_scaled)
        gb_probs = gb.predict_proba(X_test_scaled)

        # Ensure both classifiers have 2 classes
        if rf_probs.shape[1] == 2 and gb_probs.shape[1] == 2:
            ensemble_probs = (rf_probs + gb_probs) / 2
            y_pred = (ensemble_probs[:, 1] >= 0.5).astype(int)
        else:
            y_pred = rf.predict(X_test_scaled)

        # Metrics
        accuracy = accuracy_score(y_test, y_pred)
        precision = precision_score(y_test, y_pred, zero_division=0)
        recall = recall_score(y_test, y_pred, zero_division=0)
        f1 = f1_score(y_test, y_pred, zero_division=0)
        cm = confusion_matrix(y_test, y_pred)

        # Feature importance
        feature_importance = {}
        if hasattr(rf, 'feature_importances_') and self._feature_names.get(symbol):
            importances = rf.feature_importances_
            feature_importance = dict(zip(
                self._feature_names[symbol][:len(importances)],
                importances
            ))
            feature_importance = dict(sorted(
                feature_importance.items(),
                key=lambda x: x[1],
                reverse=True
            ))

        # Store per-symbol models
        self._models[symbol] = {'rf': rf, 'gb': gb}
        self._scalers[symbol] = scaler
        self._training_accuracies[symbol] = accuracy
        self._last_train_times[symbol] = datetime.now()
        self._model_loaded[symbol] = True
        self._feature_importances[symbol] = feature_importance

        # Log results
        self.log.info(f"[{symbol}] ML Training Complete — Accuracy: {accuracy:.3f}, Precision: {precision:.3f}, "
                     f"Recall: {recall:.3f}, F1: {f1:.3f}")
        self.log.info(f"[{symbol}] Confusion Matrix: {cm.tolist()}")

        if feature_importance:
            top_features = list(feature_importance.keys())[:5]
            self.log.info(f"[{symbol}] Top features: {[(f, round(feature_importance[f], 3)) for f in top_features]}")

        # Save
        self.save_model(symbol)

        return {
            'status': 'success',
            'symbol': symbol,
            'accuracy': accuracy,
            'precision': precision,
            'recall': recall,
            'f1': f1,
            'samples': len(X),
            'up_samples': int(n_pos),
            'down_samples': int(n_neg),
            'top_features': top_features if feature_importance else []
        }

    def predict(self, df: pd.DataFrame, symbol: str = "BTC/USDT") -> Dict:
        """Predict market direction using trained model for a given symbol."""
        default = {
            'signal': 'HOLD',
            'confidence': 0.0,
            'prob_up': 0.5,
            'prob_down': 0.5,
            'predicted_change_pct': 0.0,
            'feature_importance': self._feature_importances.get(symbol, {}),
            'symbol': symbol
        }

        model_loaded = self._model_loaded.get(symbol, False)
        model = self._models.get(symbol)
        scaler = self._scalers.get(symbol)

        if not model_loaded or model is None or scaler is None:
            # Try to load from disk
            self.load_model(symbol)
            model = self._models.get(symbol)
            scaler = self._scalers.get(symbol)
            if model is None or scaler is None:
                return default

        try:
            features = self.prepare_features(df, symbol)
            if features.empty:
                return default

            X = features.values[-1:].reshape(1, -1)

            # Check feature count matches
            if hasattr(scaler, 'n_features_in_') and X.shape[1] != scaler.n_features_in_:
                self.log.warning(f"[{symbol}] Feature mismatch: got {X.shape[1]}, expected {scaler.n_features_in_}")
                return default

            X_scaled = scaler.transform(X)

            # Ensemble prediction
            rf = model.get('rf')
            gb = model.get('gb')

            if rf is None or gb is None:
                return default

            rf_prob = rf.predict_proba(X_scaled)
            gb_prob = gb.predict_proba(X_scaled)

            if rf_prob.shape[1] == 2 and gb_prob.shape[1] == 2:
                prob_up = (rf_prob[0][1] + gb_prob[0][1]) / 2
                prob_down = (rf_prob[0][0] + gb_prob[0][0]) / 2
            else:
                prob_up = rf_prob[0][1] if rf_prob.shape[1] == 2 else 0.5
                prob_down = 1 - prob_up

            confidence = max(prob_up, prob_down)

            if prob_up > 0.62:
                signal = 'BUY'
            elif prob_down > 0.62:
                signal = 'SELL'
            else:
                signal = 'HOLD'

            # Estimate predicted change (rough based on confidence)
            predicted_change = (prob_up - prob_down) * 2.0  # rough estimate in %

            return {
                'signal': signal,
                'confidence': confidence,
                'prob_up': float(prob_up),
                'prob_down': float(prob_down),
                'predicted_change_pct': float(predicted_change),
                'feature_importance': self._feature_importances.get(symbol, {}),
                'symbol': symbol
            }

        except Exception as e:
            self.log.error(f"[{symbol}] Prediction error: {e}")
            return default

    def update(self, df: pd.DataFrame, symbol: str = "BTC/USDT"):
        """Incremental update (retrain) for a given symbol."""
        if self._model_loaded.get(symbol, False) and len(df) > 100:
            self.train(df, symbol)

    def save_model(self, symbol: str = "BTC/USDT"):
        """Save model and scaler for a given symbol to disk."""
        model_path = self._get_model_path(symbol)
        scaler_path = self._get_scaler_path(symbol)

        try:
            model = self._models.get(symbol)
            scaler = self._scalers.get(symbol)
            if model is not None:
                joblib.dump(model, model_path)
            if scaler is not None:
                joblib.dump(scaler, scaler_path)
            self.log.info(f"[{symbol}] Model saved to {model_path}")
        except Exception as e:
            self.log.error(f"[{symbol}] Failed to save model: {e}")

    def load_model(self, symbol: str = "BTC/USDT"):
        """Load model and scaler for a given symbol from disk."""
        model_path = self._get_model_path(symbol)
        scaler_path = self._get_scaler_path(symbol)

        try:
            model = joblib.load(model_path)
            scaler = joblib.load(scaler_path)
            self._models[symbol] = model
            self._scalers[symbol] = scaler
            self._model_loaded[symbol] = True
            self.log.info(f"[{symbol}] Model loaded from {model_path}")
        except Exception as e:
            self.log.warning(f"[{symbol}] Failed to load model: {e}")
            self._model_loaded[symbol] = False
