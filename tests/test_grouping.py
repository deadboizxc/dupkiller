"""Tests for dupkiller.grouping — group_by_size, group_by_hash."""
from dupkiller.grouping import group_by_hash, group_by_size
from dupkiller.utils import FileInfo


def _fi(path: str, size: int) -> FileInfo:
    return FileInfo(path=path, size=size, mtime=1.0, inode=1, device=1)


class TestGroupBySize:
    def test_groups_same_size(self):
        files = [_fi("/a", 100), _fi("/b", 100), _fi("/c", 200)]
        result = group_by_size(files)
        assert 100 in result
        assert len(result[100]) == 2
        assert 200 not in result  # singleton excluded

    def test_empty_input(self):
        assert group_by_size([]) == {}

    def test_all_unique_sizes(self):
        files = [_fi(f"/{i}", i * 10 + 1) for i in range(5)]
        assert group_by_size(files) == {}

    def test_all_same_size(self):
        files = [_fi(f"/{i}", 512) for i in range(4)]
        result = group_by_size(files)
        assert len(result[512]) == 4


class TestGroupByHash:
    def test_groups_same_hash(self):
        pairs = [("/a", "aaa"), ("/b", "aaa"), ("/c", "bbb")]
        result = group_by_hash(pairs)
        assert "aaa" in result
        assert set(result["aaa"]) == {"/a", "/b"}
        assert "bbb" not in result  # singleton excluded

    def test_none_hash_dropped(self):
        pairs = [("/a", "aaa"), ("/b", None), ("/c", "aaa")]
        result = group_by_hash(pairs)
        assert set(result["aaa"]) == {"/a", "/c"}

    def test_empty_input(self):
        assert group_by_hash([]) == {}

    def test_all_none_hashes(self):
        pairs = [("/a", None), ("/b", None)]
        assert group_by_hash(pairs) == {}
