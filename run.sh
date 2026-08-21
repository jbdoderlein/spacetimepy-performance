
# Discord.py

cd discord.py
sed -i 's/requires-python = ">=3\.8"/requires-python = ">=3.12"/' pyproject.toml
uv venv
uv sync --all-extras
uv run pytest > ../results/raw/discordpy_without_monitoring
cp ../modification/discord.py/conftest.py tests/conftest.py
echo "spacetimepy @ git+https://github.com/jbdoderlein/SpaceTimePy" >> requirements.txt
uv sync --all-extras
uv run pytest > ../results/raw/discordpy_with_monitoring
cp performance.db ../results/raw/discordpy_db