"""
Pattern Analyzer: Train ML model on 11 scenarios to detect conflicts.
Extracts features from flights, detects conflicts (separation loss),
trains RandomForest to predict future conflicts.
"""
import json
import glob
from pathlib import Path
from datetime import datetime, timedelta
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
import pickle
from dataclasses import dataclass

@dataclass
class Flight:
    flight_number: str
    origin: str
    destination: str
    cruise_alt: int
    cruise_speed: int
    lats: list
    lons: list
    is_airborne: bool
    take_off_time: str

def load_scenario(scenario_path):
    """Load all flights from a scenario."""
    routes_file = Path(scenario_path) / "routes.json"
    if not routes_file.exists():
        return []

    with open(routes_file) as f:
        data = json.load(f)

    flights = []
    for flight_data in data.get("flights", []):
        flights.append(Flight(
            flight_number=flight_data.get("flight_number", "UNK"),
            origin=flight_data.get("origin_airport_icao", ""),
            destination=flight_data.get("destination_airport_icao", ""),
            cruise_alt=flight_data.get("cruise_altitude_ft", 0),
            cruise_speed=flight_data.get("cruise_speed_kt", 0),
            lats=flight_data.get("lats", []),
            lons=flight_data.get("lons", []),
            is_airborne=flight_data.get("is_airborne", False),
            take_off_time=flight_data.get("take_off_time", "")
        ))
    return flights

def haversine_distance(lat1, lon1, lat2, lon2):
    """Distance in nautical miles."""
    R = 3440.06  # Earth radius in NM
    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat/2)**2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon/2)**2
    c = 2 * np.arcsin(np.sqrt(a))
    return R * c

def detect_conflicts(flights, min_horizontal_nm=3, min_vertical_ft=1000):
    """
    Detect pairs of flights that violate separation minima.
    Returns list of (flight1_idx, flight2_idx, severity).
    """
    conflicts = []
    for i in range(len(flights)):
        for j in range(i+1, len(flights)):
            f1, f2 = flights[i], flights[j]

            if not (f1.is_airborne and f2.is_airborne):
                continue
            if not (f1.lats and f2.lats):
                continue

            # Use last waypoint (closest to current)
            lat1, lon1 = f1.lats[-1], f1.lons[-1]
            lat2, lon2 = f2.lats[-1], f2.lons[-1]

            horiz_dist = haversine_distance(lat1, lon1, lat2, lon2)
            vert_dist = abs(f1.cruise_alt - f2.cruise_alt)

            # CONUS separation minima: 5nm/1000ft
            if horiz_dist < min_horizontal_nm and vert_dist < min_vertical_ft:
                severity = max(0, 10 - horiz_dist) * max(0, 10 - vert_dist/1000)
                conflicts.append((i, j, severity))

    return conflicts

def extract_features(flight, flight_idx, all_flights, conflicts_set):
    """Extract ML features for a flight."""
    # Basic features
    cruise_alt_norm = min(flight.cruise_alt / 45000, 1.0)
    cruise_speed_norm = min(flight.cruise_speed / 500, 1.0)

    # Time features
    try:
        takeoff = datetime.fromisoformat(flight.take_off_time.replace('+00:00', ''))
        hour = takeoff.hour
        month = takeoff.month
        day_of_week = takeoff.weekday()
    except:
        hour = 12
        month = 6
        day_of_week = 0

    # Position features
    if flight.lats:
        lat_mean = np.mean(flight.lats)
        lon_mean = np.mean(flight.lons)
        lat_std = np.std(flight.lats) if len(flight.lats) > 1 else 0
    else:
        lat_mean, lon_mean, lat_std = 35, -100, 0

    # Distance traveled
    distance = 0
    if len(flight.lats) > 1:
        for k in range(len(flight.lats)-1):
            distance += haversine_distance(
                flight.lats[k], flight.lons[k],
                flight.lats[k+1], flight.lons[k+1]
            )

    # Conflict indicator (1 if this flight was in a conflict, else 0)
    has_conflict = any((f_idx == flight_idx or s_idx == flight_idx) for f_idx, s_idx, _ in conflicts_set)

    return [
        cruise_alt_norm,
        cruise_speed_norm,
        hour / 24,
        month / 12,
        day_of_week / 7,
        lat_mean / 90,
        lon_mean / 180,
        lat_std / 10,
        distance / 5000,
    ], int(has_conflict)

def train_pattern_model():
    """Train ML model on all 11 scenarios."""
    print("Loading all scenarios...")
    scenarios_dir = Path("data/scenarios")
    scenario_paths = sorted(glob.glob(str(scenarios_dir / "asked_at_*")))

    X_all = []
    y_all = []

    for scenario_path in scenario_paths:
        scenario_name = Path(scenario_path).name
        print(f"  Processing {scenario_name}...")

        flights = load_scenario(scenario_path)
        if not flights:
            print(f"    Skipped (no flights)")
            continue

        print(f"    Loaded {len(flights)} flights")

        # Detect conflicts
        conflicts = detect_conflicts(flights)
        conflicts_set = set([(i, j, s) for i, j, s in conflicts] + [(j, i, s) for i, j, s in conflicts])

        print(f"    Found {len(conflicts)} conflicts")

        # Extract features for each flight
        for flight_idx, flight in enumerate(flights[:5000]):  # Sample to speed up
            features, label = extract_features(flight, flight_idx, flights, conflicts_set)
            X_all.append(features)
            y_all.append(label)

    X = np.array(X_all)
    y = np.array(y_all)

    print(f"\nTraining on {len(X)} samples...")
    print(f"Positive class: {y.sum()} conflicts, Negative: {len(y) - y.sum()} safe")

    # Normalize features
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Train model
    model = RandomForestClassifier(n_estimators=50, max_depth=10, random_state=42)
    model.fit(X_scaled, y)

    print(f"Model accuracy: {model.score(X_scaled, y):.2%}")
    print(f"Feature importance: {dict(zip(range(len(model.feature_importances_)), model.feature_importances_))}")

    # Save model
    model_dir = Path("data/models")
    model_dir.mkdir(exist_ok=True)

    with open(model_dir / "conflict_model.pkl", "wb") as f:
        pickle.dump((model, scaler), f)

    print(f"\nModel saved to {model_dir / 'conflict_model.pkl'}")
    return model, scaler

if __name__ == "__main__":
    train_pattern_model()
