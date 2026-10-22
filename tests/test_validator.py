from runtime.validator import flatten_facts


def test_wrapper_key_is_flattened():
    assert flatten_facts({"context_yaml": {"task_titles": ["a"], "done_title": "a"}, "extra": 1}) == {
        "task_titles": ["a"], "done_title": "a", "extra": 1,
    }


def test_top_level_wins_over_wrapper():
    assert flatten_facts({"context": {"k": "inner"}, "k": "outer"})["k"] == "outer"


def test_non_dict_wrapper_is_dropped_not_crashed():
    assert flatten_facts({"context": "not a dict", "k": 1}) == {"k": 1}


def test_no_wrapper_passthrough():
    assert flatten_facts({"a": 1}) == {"a": 1}
