"""Show two deterministic cwd errors using the real local shell."""
from test_acceptance import BashCwdAcceptance, get_tracked_cwd, set_tracked_cwd


def main():
    fixture = BashCwdAcceptance()
    fixture.setUp()
    try:
        for command, expected in (("cd sub && false", fixture.root / "sub"),
                                  ("false && cd sub; true", fixture.root)):
            set_tracked_cwd(str(fixture.root))
            print("命令:", command)
            print("输出:", fixture.bash.execute(command))
            print("记录目录:", get_tracked_cwd())
            print("下一条 pwd:", fixture.bash.execute("pwd"))
            print("期望目录:", expected)
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    main()
