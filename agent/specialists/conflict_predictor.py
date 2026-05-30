"""
Conflict Predictor Agent: Detects separation violations using geometry.
Projects aircraft trajectories and checks for loss of separation.
"""
from agent.specialists.base import Specialist, Finding, Manifest
import numpy as np


class ConflictPredictorAgent(Specialist):
    """Detects conflicts using trajectory extrapolation."""

    manifest = Manifest(
        name="conflict_predictor",
        description="Geometric conflict detection via trajectory extrapolation",
        model="claude-haiku-4-5-20251001",
        system_prompt="""You are a separation standards expert.
You detect conflicts by projecting aircraft forward and checking separation.
CONUS minima: 5nm horizontal / 1000ft vertical.
Be specific: aircraft callsigns, time to conflict, recommended action.""",
        tool_refs=[],
        interests=["user.question"],
    )

    def __init__(self):
        super().__init__()
        self.active_conflicts = []

    def predict_conflicts(self, flight_list, minutes_ahead=30):
        """Check all flight pairs for predicted conflicts."""
        conflicts = []

        for i in range(len(flight_list)):
            for j in range(i + 1, len(flight_list)):
                conflict = self._check_pair(
                    flight_list[i], flight_list[j], minutes_ahead
                )
                if conflict:
                    conflicts.append(conflict)

        self.active_conflicts = conflicts
        return conflicts

    def _check_pair(self, f1, f2, minutes_ahead):
        """Check if two flights will violate separation."""
        try:
            # Current positions (last waypoint)
            lat1, lon1 = f1.get("lats", [0])[-1], f1.get("lons", [0])[-1]
            lat2, lon2 = f2.get("lats", [0])[-1], f2.get("lons", [0])[-1]

            alt1 = f1.get("cruise_altitude_ft", 25000)
            alt2 = f2.get("cruise_altitude_ft", 25000)

            # Project forward (simple: same heading, speed)
            heading1 = self._get_heading(f1)
            heading2 = self._get_heading(f2)
            speed1 = f1.get("cruise_speed_kt", 400)
            speed2 = f2.get("cruise_speed_kt", 400)

            # Extrapolate positions
            lat1_future, lon1_future = self._extrapolate(
                lat1, lon1, heading1, speed1, minutes_ahead
            )
            lat2_future, lon2_future = self._extrapolate(
                lat2, lon2, heading2, speed2, minutes_ahead
            )

            # Check separation
            horiz_dist = self._haversine(lat1_future, lon1_future, lat2_future, lon2_future)
            vert_dist = abs(alt1 - alt2)

            # CONUS minima
            if horiz_dist < 5 and vert_dist < 1000:
                return {
                    "flight1": f1.get("flight_number", "UNK1"),
                    "flight2": f2.get("flight_number", "UNK2"),
                    "horiz_nm": round(horiz_dist, 1),
                    "vert_ft": round(vert_dist),
                    "time_to_conflict_min": minutes_ahead,
                    "severity": max(0, (5 - horiz_dist) * (1000 - vert_dist) / 1000),
                }

            return None
        except:
            return None

    def _get_heading(self, flight):
        """Estimate heading from lat/lon sequence."""
        lats = flight.get("lats", [])
        lons = flight.get("lons", [])

        if len(lats) < 2:
            return 0

        dlat = lats[-1] - lats[0]
        dlon = lons[-1] - lons[0]
        heading = np.degrees(np.arctan2(dlon, dlat))
        return heading % 360

    def _extrapolate(self, lat, lon, heading, speed_kt, minutes):
        """Project position forward."""
        # Distance traveled (nm)
        distance_nm = speed_kt * minutes / 60

        # Convert to radians
        heading_rad = np.radians(heading)
        lat_rad = np.radians(lat)
        lon_rad = np.radians(lon)

        # Earth radius in nm
        R = 3440.06

        # Forward projection (great circle)
        lat_future_rad = np.arcsin(
            np.sin(lat_rad) * np.cos(distance_nm / R)
            + np.cos(lat_rad) * np.sin(distance_nm / R) * np.cos(heading_rad)
        )
        lon_future_rad = lon_rad + np.arctan2(
            np.sin(heading_rad) * np.sin(distance_nm / R) * np.cos(lat_rad),
            np.cos(distance_nm / R) - np.sin(lat_rad) * np.sin(lat_future_rad),
        )

        return np.degrees(lat_future_rad), np.degrees(lon_future_rad)

    def _haversine(self, lat1, lon1, lat2, lon2):
        """Distance in nautical miles."""
        R = 3440.06
        lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
        c = 2 * np.arcsin(np.sqrt(a))
        return R * c

    def formulate(self, event, context=None):
        """Generate conflict findings from traffic events or user questions."""
        if event.type == "user.question":
            # User is asking about conflicts
            user_msg = event.payload.get("text", "").lower()
            if "conflict" in user_msg or "separation" in user_msg or "developing" in user_msg:
                if self.active_conflicts:
                    yield Finding(
                        specialist="conflict_predictor",
                        summary=f"Monitoring {len(self.active_conflicts)} potential separation loss events",
                        detail=f"Using 30-minute trajectory extrapolation with 5nm/1000ft separation minima",
                        severity=2,
                        metadata={"total_conflicts_tracked": len(self.active_conflicts)}
                    )
                else:
                    yield Finding(
                        specialist="conflict_predictor",
                        summary="No predicted separation violations in current traffic snapshot",
                        severity=0,
                        metadata={"prediction_horizon_min": 30}
                    )
            return

        # React to active conflicts on the bus
        if not self.active_conflicts:
            return

        for conflict in self.active_conflicts:
            if conflict.get("severity", 0) >= 4:  # High severity
                yield Finding(
                    specialist="conflict_predictor",
                    summary=f"Predicted separation loss: {conflict['flight1']} & {conflict['flight2']}",
                    detail=f"{conflict.get('horiz_nm', 0):.1f}nm apart in {conflict.get('time_to_conflict_min', 0):.0f}min",
                    severity=4,
                    metadata=conflict,
                    map_actions=[
                        {"type": "highlight_flight", "flight": conflict["flight1"]},
                        {"type": "highlight_flight", "flight": conflict["flight2"]},
                    ]
                )

    def inject_llm(self, llm_call):
        """Enable LLM mode."""
        super().inject_llm(llm_call)
