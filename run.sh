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
    reset_env
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
    reset_env
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
    reset_env
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
    reset_env
    cd ..
}

test_pymisp(){
    # pymisp
    cd pymisp

    echo "pymisp tests"
    # without spacetime
    reset_env
    git submodule update --init
    sed -i 's/requires-python = ">=3\.10,<4\.0"/requires-python = ">=3.12"/' pyproject.toml
    uv add pytest
    setup_pyenv
    uv run pytest > ../results/raw/pymisp_wtm

    reset_env
    git submodule update --init
    sed -i 's/requires-python = ">=3\.10,<4\.0"/requires-python = ">=3.12"/' pyproject.toml
    uv add git+https://github.com/jbdoderlein/SpaceTimePy
    uv add pytest
    cp ../modification/pymisp/conftest.py tests/conftest.py
    cp ../modification/pymisp/spacetimepy_custom_pickler.py spacetimepy_custom_pickler.py
    setup_pyenv
    uv run pytest > ../results/raw/pymisp_wm_wf
    mv performance.db ../results/raw/pymisp_db_prof.db
    reset_env
    cd ..
}


#test_discordpy
test_beets
#test_cherrypy
#test_dspy
#test_pymisp


