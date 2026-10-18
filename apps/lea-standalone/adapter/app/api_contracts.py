"""SQL-only route adapters, without legacy functions or execution guards."""
from functools import wraps
import inspect
from fastapi import HTTPException
from . import db
from .core_query_dispatch import _query


def sql_query(kind):
    def decorate(contract):
        signature=inspect.signature(contract,eval_str=True)
        @wraps(contract)
        def read(*args,**kwargs):
            bound=signature.bind(*args,**kwargs);bound.apply_defaults()
            try:
                with db.read_snapshot():return _query(kind,dict(bound.arguments))
            except LookupError as exc:raise HTTPException(404,str(exc)) from exc
            except ValueError as exc:raise HTTPException(422,str(exc)) from exc
        read.__signature__=signature
        return read
    return decorate
