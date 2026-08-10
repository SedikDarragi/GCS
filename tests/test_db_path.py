import os

from main import DB_PATH


def test_db_path_is_absolute():
    assert os.path.isabs(DB_PATH)


def test_db_path_lives_next_to_scripts():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.dirname(DB_PATH) == project_root
    assert os.path.basename(DB_PATH) == "commercial.db"
