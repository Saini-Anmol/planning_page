"""
Flask app entry point. Run with:

    python app.py

Listens on http://localhost:5001. Two endpoints are registered:
    POST http://localhost:5001/app/v1/jkt/planning-scheduling/plan/generate-plan
         → planning page  (reads/writes jkt_* tables)
    POST http://localhost:5001/app/v1/jkt/planning-scheduling/simulation/generate-plan
         → simulation page (reads/writes jkt_sim_* tables)
Body for both: {"plan_id": "<id>"}
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on sys.path so V1.* and simulation.* imports resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from flask import Flask

from V1.routes.api_route import bp as planning_bp
from simulation.routes.api_route import bp as simulation_bp


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(planning_bp)
    app.register_blueprint(simulation_bp)
    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
