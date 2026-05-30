"""
Wind Router Agent: Optimizes routing using wind data.
Recommends altitude/routing changes to save fuel and time.
"""
from agent.specialists.base import Specialist, Finding, Manifest
import numpy as np
from datetime import datetime


class WindRouterAgent(Specialist):
    """Optimizes routing based on wind conditions."""

    manifest = Manifest(
        name="wind_router",
        description="Route optimization using wind data and altitude suggestions",
        model="claude-haiku-4-5-20251001",
        system_prompt="""You are a route optimization expert.
You use wind data to recommend altitude/routing changes.
Focus on fuel savings and time benefits.
Be specific: recommended altitude, expected fuel savings, time saved.""",
        tool_refs=[],
        interests=["user.question"],
    )

    def __init__(self):
        super().__init__()
        # Cached wind profiles (simplified for hackathon)
        self.wind_profiles = self._load_wind_profiles()

    def _load_wind_profiles(self):
        """Load simplified wind data by altitude/time."""
        return {
            "summer_jet_stream": {
                "months": [6, 7, 8],
                "altitudes": [35000, 41000],  # FL350-FL410
                "wind_component": 25,  # knots tailwind
                "fuel_savings_pct": 8,
            },
            "winter_polar_route": {
                "months": [12, 1, 2],
                "altitudes": [39000, 43000],
                "wind_component": 35,
                "fuel_savings_pct": 10,
            },
            "spring_turbulent": {
                "months": [3, 4, 5],
                "altitudes": [31000, 35000],
                "wind_component": -5,  # headwind
                "fuel_savings_pct": 0,  # avoid, use lower alt
            },
        }

    def analyze_routing(self, flight_data):
        """Analyze flight routing and suggest improvements."""
        try:
            cruise_alt = flight_data.get("cruise_altitude_ft", 25000)
            origin = flight_data.get("origin_airport_icao", "")
            destination = flight_data.get("destination_airport_icao", "")

            # Determine current month
            try:
                takeoff = datetime.fromisoformat(
                    flight_data.get("take_off_time", "").replace("+00:00", "")
                )
                month = takeoff.month
            except:
                month = 6

            # Check wind profiles
            recommendation = None
            for profile_name, profile in self.wind_profiles.items():
                if month in profile["months"]:
                    recommended_alt = profile["altitudes"][0]

                    if profile["wind_component"] > 0:  # Tailwind available
                        if cruise_alt < recommended_alt:
                            fuel_savings = profile["fuel_savings_pct"]
                            recommendation = {
                                "current_alt": cruise_alt,
                                "recommended_alt": recommended_alt,
                                "wind_component": profile["wind_component"],
                                "fuel_savings_pct": fuel_savings,
                                "time_savings_min": round(
                                    profile["wind_component"]
                                    / 400
                                    * 60
                                    * 2  # rough estimate for 2h flight
                                ),
                                "reason": profile_name,
                            }

            return recommendation

        except Exception as e:
            return None

    def formulate(self, event, context=None):
        """Generate routing recommendations from flight events or user questions."""
        if event.type == "user.question":
            # User is asking about wind routing - give general assessment
            user_msg = event.payload.get("text", "").lower()
            if "wind" in user_msg or "routing" in user_msg or "altitude" in user_msg:
                yield Finding(
                    specialist="wind_router",
                    summary="Current wind patterns favor FL350+ for eastbound traffic, FL250-300 for westbound",
                    detail="Seasonal jet stream positioning offers 15-25kt tailwind advantage at altitude",
                    severity=1,
                    recommended_action="Consider climb-out to capture tailwind benefit",
                    metadata={"current_month": 5},
                )
            return

        # Specific flight data
        flight = event.payload.get("flight")
        if not flight:
            return

        recommendation = self.analyze_routing(flight)
        if recommendation and recommendation.get("wind_component", 0) > 0:
            yield Finding(
                specialist="wind_router",
                summary=f"Climb to FL{recommendation['recommended_alt']//100} for tailwind optimization",
                detail=f"+{recommendation['wind_component']}kt tailwind, save ~{recommendation['fuel_savings_pct']}% fuel",
                severity=1,  # Advisory
                recommended_action=f"Request climb to FL{recommendation['recommended_alt']//100}",
                metadata=recommendation,
            )

    def inject_llm(self, llm_call):
        """Enable LLM mode."""
        super().inject_llm(llm_call)
