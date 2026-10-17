"""The final code comes out of the result as one solution.py, stored with the run and downloadable."""
import time

from fastapi.testclient import TestClient

from app.codefile import find
from app.config import Settings
from app.main import create_app

SOLUTION = "import numpy as np\n\n\ndef main():\n    print(np.arange(3))\n\n\nmain()\n"
DRY_RUN = "import solution\n\nsolution.main()\nprint('dry run done')\n"


def test_the_block_labelled_solution_py_is_the_file():
    by_heading = f"# Result\n\nIntro.\n\n## solution.py\n```python\n{SOLUTION}```\n\n## dry_run.py\n```python\n{DRY_RUN * 5}```\n"
    found = find(by_heading)
    assert found == {"name": "solution.py", "code": SOLUTION, "lines": SOLUTION.count("\n")}

    by_info = f"Some text\n```python title=solution.py\n{SOLUTION}```\n"
    assert find(by_info)["code"] == SOLUTION
    assert find(f"**solution.py**\n\n~~~py\n{SOLUTION}~~~\n")["code"] == SOLUTION, "tilde fences and bold labels"


def test_without_a_label_the_largest_python_block_is_used_but_not_helpers():
    small = "import os\nprint(os.getcwd())\nprint(1)\n"
    text = (f"```bash\npip install numpy\npython solution.py\nls\n```\n\n```python\n{small}```\n\n"
            f"```python\n{SOLUTION}```\n\n### dry_run.py\n```python\n{DRY_RUN * 10}```\n")
    assert find(text)["code"] == SOLUTION, "the bash block and dry_run.py are skipped"
    assert find(text, name="solution.py")["name"] == "solution.py"
    assert find(f"### train.py\n```python\n{SOLUTION}```")["name"] == "train.py", "a labelled file keeps its own name"
    assert find(f"```\n{SOLUTION}```")["code"] == SOLUTION, "an unlabelled block that reads as Python counts"


def test_prose_results_and_tiny_snippets_have_no_file():
    snippet = f"An example:\n\n```python\n{SOLUTION}```\n"
    assert find(snippet, any_python=False) is None, "a research answer's example is not the deliverable"
    assert find(f"## solution.py\n```python\n{SOLUTION}```", any_python=False)["name"] == "solution.py"
    assert find("```python\nprint(1)\n```") is None
    assert find("No code here.") is None and find("") is None


def test_a_result_cut_off_inside_the_code_still_gives_the_code():
    found = find(f"## solution.py\n```python\n{SOLUTION}")
    assert found and found["code"] == SOLUTION


def wait_for(client: TestClient, run_id: str) -> dict:
    end = time.time() + 15
    while time.time() < end:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in ("completed", "failed"):
            return run
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_an_eris_run_offers_solution_py_for_download():
    settings = Settings(_env_file=None, allow_simulated=True, simulated_delay=0.0, static_dir="/nonexistent")
    with TestClient(create_app(settings)) as client:
        run = client.post("/api/runs", json={"request": "Solve this Eris challenge", "eris": True,
                                             "require_plan_approval": False}).json()
        run = wait_for(client, run["id"])
        assert run["status"] == "completed", run.get("error")
        sol = run["solution"]
        assert sol["name"] == "solution.py" and "def main():" in sol["code"] and "import solution" not in sol["code"]

        r = client.get(f"/api/runs/{run['id']}/solution")
        assert r.status_code == 200 and r.text == sol["code"]
        assert r.headers["content-disposition"] == "attachment; filename*=UTF-8''solution.py"
        assert r.headers["content-type"].startswith("text/x-python")

        plain = client.post("/api/runs", json={"request": "Summarise the history of tea", "require_plan_approval": False}).json()
        plain = wait_for(client, plain["id"])
        assert plain["solution"] is None
        assert client.get(f"/api/runs/{plain['id']}/solution").status_code == 404
