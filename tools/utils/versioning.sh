function showGitDiff () {
    echo "diff since $(git log --format="%H" -n 1) commit:"  # last commit it ...
    echo $(git diff)  # ... and differences
}