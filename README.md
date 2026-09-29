<p align="center">
    <a href="https://www.witboost.com/">
        <img src="docs/img/witboost_logo.svg" alt="witboost" width=600 >
    </a>
</p>

Designed by [Agile Lab](https://www.agilelab.it/), Witboost is a versatile platform that addresses a wide range of sophisticated data engineering challenges. It enables businesses to discover, enhance, and productize their data, fostering the creation of automated data platforms that adhere to the highest standards of data governance. Want to know more about Witboost? Check it out [here](https://www.witboost.com/) or [contact us!](https://witboost.com/contact-us)

# Soda Data Quality Tech Adapter

- [Overview](#overview)
- [Building](#building)
- [Running](#running)
- [OpenTelemetry Setup](tech-adapter/docs/opentelemetry.md)
- [Deploying](#deploying)
- [Documentation](#documentation)

## Overview

This Tech Adapter provisions and unprovisions Soda Cloud data quality monitoring for output ports. It is invoked by Witboost for workload components whose `infrastructureTemplateId` is `urn:dmb:itm:soda-tech-adapter:0`.

Given a Data Product descriptor, it reads the Soda workload component's target output port(s), resolves the corresponding tables/views in Soda Cloud (via Data Source discovery and dataset onboarding), and creates/publishes a Soda contract encoding the output port's `dataContract.quality` checks (in [Soda Contract Language](https://docs.soda.io/reference/contract-language-reference)). On unprovisioning, it clears the Soda contract associated with the dataset.

See [docs/HLD.md](docs/HLD.md) for the architecture and [docs/technical-details.md](docs/technical-details.md) for the full phase-by-phase provisioning/unprovisioning flow, error codes, and configuration reference.

### What's a Tech Adapter?

A Tech Adapter is a microservice which is in charge of deploying components that use a specific technology. When the deployment of a Data Product is triggered, the platform generates its descriptor and orchestrates the deployment of every component contained in the Data Product. For every such component the platform knows which Tech Adapter is responsible for its deployment, and can thus send a provisioning request with the descriptor to it so that the Tech Adapter can perform whatever operation is required to fulfill this request and report back the outcome to the platform.

You can learn more about how Tech Adapters fit in the broader picture [here](https://docs.witboost.com/docs/p2_arch/p1_intro/#deploy-flow).

### Software stack

This microservice is written in Python 3.11, using FastAPI for the HTTP layer. Project is built with Poetry and supports packaging as Wheel and Docker image, ideal for Kubernetes deployments (which is the preferred option).

## Building

**Requirements:**

- Python ~3.11 (this is a **strict** requirement as of now, due to uvloop 0.17.0)
- Poetry

If you don't have any instance of Python 3 installed, you can install Python 3.11 directly executing the following commands:

```shell
sudo apt update
sudo apt install software-properties-common
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install python3.11
python3.11 --version
which python3.11
```

If you do have Python 3 installed, but not version 3.11 required by this project, we recommend using _pyenv_ to manage python environments with different versions, as it is flexible and fully compatible with Poetry. You can install _pyenv_ following the guide [here](https://github.com/pyenv/pyenv?tab=readme-ov-file#installation). Then, to install and configure to use Python 3.11 on the project simply execute:

```shell
pyenv install 3.11
cd tech-adapter
pyenv local 3.11
```

**Installing**:

To set up a Python environment we use [Poetry](https://python-poetry.org/docs/):

```
curl -sSL https://install.python-poetry.org | python3 -
```

Once Poetry is installed and in your `$PATH`, you can execute the following:

```
poetry --version
```

If you see something like `Poetry (version x.x.x)`, your installation is ready to use!

Install the dependencies defined in `tech-adapter/pyproject.toml`:

```shell
cd tech-adapter
poetry env use 3.11
poetry install
```

Note: All the following commands are to be run in the Poetry project directory with the virtualenv enabled. If you use _pyenv_ to manage multiple Python runtimes, make sure Poetry is using the right version. You can tell _pyenv_ to use the Python version available in the current shell. Check this Poetry docs page [here](https://python-poetry.org/docs/managing-environments/).

**Type check:** is handled by mypy:

```bash
poetry run mypy src/
```

**Tests:** are handled by pytest:

```bash
poetry run pytest --cov=src/ tests/. --cov-report=xml
```

**Artifacts & Docker image:** the project leverages Poetry for packaging. Build package with:

```
poetry build
```

The Docker image can be built with:

```
docker build .
```

More details can be found [here](tech-adapter/docs/docker.md).

_Note:_ the version for the project is automatically computed using information gathered from Git, using branch name and tags. Unless you are on a release branch `1.2.x` or a tag `v1.2.3` it will end up being `0.0.0`. You can follow this branch/tag convention or update the version computation to match your preferred strategy.

**CI/CD:** the pipeline is based on GitLab CI. It's configured by the `.gitlab-ci.yml` file in the root of the repository.

## Running

To run the server locally, use:

```bash
cd tech-adapter
source $(poetry env info --path)/bin/activate # only needed if venv is not already enabled
uvicorn src.main:app --host 127.0.0.1 --port 8091
```

By default, the server binds to port 8091 on localhost. After it's up and running you can make provisioning requests to this address. You can also check the API documentation served [here](http://127.0.0.1:8091/docs).

## Deploying

This microservice is meant to be deployed to a Kubernetes cluster with the included Helm chart and the scripts that can be found in the `helm` subdirectory. You can find more details [here](helm/README.md).

## Documentation

- [High-Level Design](docs/HLD.md) — architecture, component mapping, and lifecycle coverage
- [Technical Details](docs/technical-details.md) — phase-by-phase provisioning/unprovisioning flow, configuration reference, and error codes
- [Reference Descriptor](docs/reference-descriptor.yaml) — example Data Product descriptor for a Soda workload component
- [Databricks End-to-End Walkthrough](docs/databricks-end-to-end.md)

## License

This project is available under the [Apache License, Version 2.0](https://opensource.org/licenses/Apache-2.0); see [LICENSE](LICENSE) for full details.

## About Witboost

[Witboost](https://witboost.com/) is a cutting-edge Data Experience platform, that streamlines complex data projects across various platforms, enabling seamless data production and consumption. This unified approach empowers you to fully utilize your data without platform-specific hurdles, fostering smoother collaboration across teams.

It seamlessly blends business-relevant information, data governance processes, and IT delivery, ensuring technically sound data projects aligned with strategic objectives. Witboost facilitates data-driven decision-making while maintaining data security, ethics, and regulatory compliance.

Moreover, Witboost maximizes data potential through automation, freeing resources for strategic initiatives. Apply your data for growth, innovation and competitive advantage.

[Contact us](https://witboost.com/contact-us) or follow us on:

- [LinkedIn](https://www.linkedin.com/showcase/witboost/)
- [YouTube](https://www.youtube.com/@witboost-platform)
