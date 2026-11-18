"""Show the actual bytes before/after one edit, only inside a temporary directory."""
from test_acceptance import NewlineAcceptance, checkpoints, EditFileTool


def main():
    fixture = NewlineAcceptance()
    fixture.setUp()
    try:
        before = b"alpha\r\nbeta\r\n"
        path = fixture.file(before)
        result = EditFileTool().execute(str(path), "beta", "BETA")
        print("编辑结果:", result)
        print("编辑前字节:", repr(before))
        print("实际编辑后:", repr(path.read_bytes()))
        print("预期编辑后:", repr(b"alpha\r\nBETA\r\n"))
        print("换行是否保留:", path.read_bytes() == b"alpha\r\nBETA\r\n")
        checkpoints.undo()
        print("撤销恢复原始字节:", path.read_bytes() == before)
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    main()
