from pathlib import Path
def test_legacy():
 assert Path('legacy.txt').read_text() == 'ok', 'legacy broke'
