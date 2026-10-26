"""Validate built-in SOP coverage before packaging ZeroAgent."""

from setuptools import setup
from setuptools.command.build_py import build_py


class VerifySkillCatalogBuildPy(build_py):
    def run(self) -> None:
        from zero_agent.skills.manifest import load_builtin_catalog

        load_builtin_catalog()
        super().run()


setup(cmdclass={"build_py": VerifySkillCatalogBuildPy})
