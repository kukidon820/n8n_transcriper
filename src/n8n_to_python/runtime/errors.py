"""Ошибки runtime с контекстом построения и выполнения графа."""


class WorkflowRuntimeError(RuntimeError):
    """Базовая ошибка построения или выполнения workflow."""


class NoTriggerNodesError(WorkflowRuntimeError):
    """Workflow нельзя запустить автоматически: нет зарегистрированного trigger-узла."""


class UnsupportedNodeTypeError(WorkflowRuntimeError):
    """Для исполняемого узла не зарегистрирован runtime-обработчик."""


class InvalidNodeOutputError(WorkflowRuntimeError):
    """Узел вернул выходы, несовместимые с подключёнными портами графа."""


class NodeExecutionError(WorkflowRuntimeError):
    """Узел завершился ошибкой; исходное исключение сохраняется как `__cause__`."""

    def __init__(self, node_name: str):
        """Сохранить имя узла, выполнение которого завершилось ошибкой."""
        self.node_name = node_name
        super().__init__(f"Ошибка при выполнении узла {node_name!r}.")


class ExecutionLimitExceededError(WorkflowRuntimeError):
    """Runtime остановил граф, превысивший лимит вызовов узлов."""


class IncompleteMergeError(WorkflowRuntimeError):
    """Merge получил часть входов и не может сформировать корректный результат."""
