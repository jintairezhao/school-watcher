"""Navigation application -> new page -> actual source commit -> replay."""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests'),str(Path(__file__).parent)]
from check_source_execution_browser import check
from test_navigation_execution_events import NavigationExecutionEventTests

if __name__=='__main__':
    check(NavigationExecutionEventTests,ROOT/'.local/structure-facts-20261021/navigation-events-browser',
        expected_event_text='Started applying navigation results')
