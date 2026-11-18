from pathlib import Path
assert Path('answer.txt').read_text() == 'fixed'
