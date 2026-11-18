"""Reproduce different tools reading different same-named temporary files."""
from test_acceptance import ToolCwdAcceptance


def main():
    fixture = ToolCwdAcceptance()
    fixture.setUp()
    try:
        print("目录结构: note.txt=ROOT VALUE；backend/note.txt=SUB VALUE")
        print("命令工具已经成功 cd backend")
        print("bash cat note.txt:", fixture.bash.execute("cat note.txt"))
        print("read_file note.txt:", fixture.read.execute("note.txt"))
        print("预期: 两者都读取 SUB VALUE")
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    main()
