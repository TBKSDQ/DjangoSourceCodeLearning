import functools
from collections import namedtuple


def make_model_tuple(model):
    """
    Take a model or a string of the form "app_label.ModelName" and return a
    corresponding ("app_label", "modelname") tuple. If a tuple is passed in,
    assume it's a valid model tuple already and return it unchanged.
    """
    try:
        if isinstance(model, tuple):
            model_tuple = model
        elif isinstance(model, str):
            app_label, model_name = model.split(".")
            model_tuple = app_label, model_name.lower()
        else:
            model_tuple = model._meta.app_label, model._meta.model_name
        assert len(model_tuple) == 2
        return model_tuple
    except (ValueError, AssertionError):
        raise ValueError(
            "Invalid model reference '%s'. String model references "
            "must be of the form 'app_label.ModelName'." % model
        )


def resolve_callables(mapping):
    """
    Generate key/value pairs for the given mapping where the values are
    evaluated if they're callable.
    根据传入的映射对象来返回键值对(以生成器的形式)，如果键值对中的value
    为可执行对象，那么执行后再将结果返回
    """
    for k, v in mapping.items():
        yield k, v() if callable(v) else v


def unpickle_named_row(names, values):
    return create_namedtuple_class(*names)(*values)


@functools.lru_cache()
def create_namedtuple_class(*names):
    # Cache type() with @lru_cache() since it's too slow to be called for every
    # QuerySet evaluation.
    def __reduce__(self):
        return unpickle_named_row, (names, tuple(self))

    return type(
        'Row',
        (namedtuple('Row', names),),
        {'__reduce__': __reduce__, '__slots__': ()},
    )
