"""Bounded requirements views and existing-owner revision transactions."""

from ._base import Namespace, call_seam


class RequirementsNamespace(Namespace):
    """Original obligations stay frozen; proposals and receipts confer no acceptance."""

    def _invoke(self, seam, *args, **kwargs):
        self._workspace._check_open()
        def load(_stem):
            from .. import requirements_operations
            return requirements_operations
        return call_seam(load, "requirements_operations", seam, self._root, *args, **kwargs)

    def current(self, *, policy=None, run_id=None):
        return self._invoke("current", policy=policy, run_id=run_id)

    def prepare_revision(self, proposal, envelope):
        return self._invoke("prepare_revision", proposal, envelope)

    def revise(self, request):
        self._workspace._check_open()
        from ..mutation_contracts import REQUEST, checked, refuse
        request = checked(REQUEST, request)
        if request["operation"] != "requirements.revise":
            refuse("OPERATION_UNSUPPORTED", "/operation")
        return self._workspace.mutations.submit(request)
