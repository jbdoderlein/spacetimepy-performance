reset_env(){
    git reset --hard
    git clean -fxd
    uv venv --clear
    uv sync --all-extras --refresh
}

# Discord.py

cd discord.py
reset_env
# without spacetime
sed -i 's/requires-python = ">=3\.8"/requires-python = ">=3.12"/' pyproject.toml
uv run pytest > ../results/raw/discordpy_wtm

reset_env
# With spacetime + profiling
sed -i '1s/^/spacetimepy @ git+https:\/\/github.com\/jbdoderlein\/SpaceTimePy\n/' requirements.txt
cp ../modification/discord.py/conftest.py tests/conftest.py
uv run pytest > ../results/raw/discordpy_wm_wf
cp performance.db ../results/raw/discordpy_db_prof.db

reset_env
rm performance.db
# With spacetime without profilling
sed -i 's/, profile_capture=True//' tests/conftest.py
uv run pytest > ../results/raw/discordpy_wm_wtf