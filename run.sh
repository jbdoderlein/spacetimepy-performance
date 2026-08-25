reset_env(){
    git reset --hard
    git clean -fxd
}
setup_pyenv(){
    uv venv --clear
    uv sync --all-extras --refresh
}

test_discordpy(){
    # Discord.py
    echo "Discord.py Test"
    cd discord.py

    # without spacetime
    reset_env
    sed -i 's/requires-python = ">=3\.8"/requires-python = ">=3.12"/' pyproject.toml
    setup_pyenv
    uv run pytest > ../results/raw/discordpy_wtm

    # With spacetime + profiling
    reset_env
    sed -i 's/requires-python = ">=3\.8"/requires-python = ">=3.12"/' pyproject.toml
    sed -i '1s/^/spacetimepy @ git+https:\/\/github.com\/jbdoderlein\/SpaceTimePy\n/' requirements.txt
    cp ../modification/discord.py/conftest.py tests/conftest.py
    cp ../modification/discord.py/spacetimepy_custom_pickler.py discord/spacetimepy_custom_pickler.py
    setup_pyenv
    uv run pytest > ../results/raw/discordpy_wm_wf
    mv performance.db ../results/raw/discordpy_db_prof.db

    cd ..
}

test_beets(){
    # Beets
    cd beets
    echo "Beets tests"
    # without spacetime
    reset_env
    sed -i 's/requires-python = ">=3\.10,<3\.15"/requires-python = ">=3.12"/' pyproject.toml
    setup_pyenv
    uv run pytest > ../results/raw/beets_wtm

    reset_env
    sed -i 's/requires-python = ">=3\.10,<3\.15"/requires-python = ">=3.12"/' pyproject.toml
    uv add git+https://github.com/jbdoderlein/SpaceTimePy
    yes | cp -r ../modification/beets/conftest.py test/conftest.py
    cp ../modification/beets/spacetimepy_picklers.py test/spacetimepy_picklers.py
    setup_pyenv
    uv run pytest > ../results/raw/beets_wm_wf
    mv performance.db ../results/raw/beets_db_prof.db

    cd ..
}


test_cherrypy(){
    # Cherrypy
    cd cherrypy

    echo "CherryPy tests"
    # without spacetime
    reset_env
    sed -i 's/requires-python = ">= 3\.9"/requires-python = ">=3.12"/' pyproject.toml
    setup_pyenv
    uv run pytest > ../results/raw/cherrypy_wtm

    reset_env
    sed -i 's/requires-python = ">= 3\.9"/requires-python = ">=3.12"/' pyproject.toml
    uv add git+https://github.com/jbdoderlein/SpaceTimePy
    cp ../modification/cherrypy/conftest.py cherrypy/test/conftest.py
    cp ../modification/cherrypy/spacetimepy_custom_pickler.py cherrypy/test/spacetimepy_custom_pickler.py
    setup_pyenv
    uv run pytest > ../results/raw/cherrypy_wm_wf
    mv performance.db ../results/raw/cherrypy_db_prof.db
    cd ..
}

test_dspy(){
    # dspy
    cd dspy

    echo "dspy tests"
    # without spacetime
    reset_env
    sed -i 's/requires-python = ">=3\.10, <3\.15"/requires-python = ">=3.12"/' pyproject.toml
    setup_pyenv
    uv run pytest > ../results/raw/dspy_wtm

    reset_env
    sed -i 's/requires-python = ">=3\.10, <3\.15"/requires-python = ">=3.12"/' pyproject.toml
    uv add git+https://github.com/jbdoderlein/SpaceTimePy
    yes | cp ../modification/dspy/conftest.py tests/conftest.py
    cp ../modification/dspy/spacetime_dill.py spacetime_dill.py
    setup_pyenv
    uv run pytest > ../results/raw/dspy_wm_wf
    mv performance.db ../results/raw/dspy_db_prof.db
    cd ..
}


test_discordpy
test_beets
test_cherrypy
test_dspy



: '

# gensim
cd trimesh

echo "trimesh tests"
# without spacetime
reset_env
sed -i 's/requires-python = ">=3\.10"/requires-python = ">=3.12"/' pyproject.toml
setup_pyenv
uv run pytest > ../results/raw/trimesh_wtm

reset_env
sed -i 's/requires-python = ">=3\.10"/requires-python = ">=3.12"/' pyproject.toml
uv add git+https://github.com/jbdoderlein/SpaceTimePy
cp ../modification/trimesh/conftest.py tests/conftest.py
setup_pyenv
uv run pytest > ../results/raw/trimesh_wm_wf
mv performance.db ../results/raw/trimesh_db_prof.db
'