"""Convenience script to run the RoadSense Web Dashboard."""
from src.roadsense.web.server import run_web

if __name__ == "__main__":
    run_web(host="127.0.0.1", port=8000, open_browser=True)
