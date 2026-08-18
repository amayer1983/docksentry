"""Keep the part of an error that says what went wrong (#2, @famewolf).

He finally got the message the scheduled check had been swallowing:

    ⚠️ Host dock8520 could not be checked: could not list containers
    (rc=1): error during connect: Get "http://docker.example.com/v1.43/
    containers/json": command ssh -l root -o ConnectTimeout=30 -T --
    dock8520.lan docker system dial-stdio ha

— and it stops there. Two hundred characters, cut exactly where the
reason begins: `…dial-stdio has exited with exit status 255, stderr=ssh:
Permission denied (publickey)`. We kept the preamble and threw away the
diagnosis.

That is not specific to Docker. Wrapped errors put the context first and
the cause last, all the way down: an HTTP client wraps a transport error
wraps a subprocess failure wraps the one line somebody can act on. Taking
the first N characters of that is taking the least useful N characters.

So: keep both ends. The head says what was being attempted, the tail says
why it failed, and the middle — which is usually a URL and an argument
list — is what goes.
"""


def clip(err, limit=400, head=140):
    """`err` as a single line, trimmed from the middle if it is long.

    `head` characters of context, the rest of the budget spent on the
    end, and an ellipsis marking the join so nobody reads the result as
    one continuous sentence.
    """
    text = " ".join(str(err).split())
    if len(text) <= limit:
        return text
    head = max(0, min(head, limit - 40))
    tail = limit - head - 5
    return f"{text[:head]} […] {text[-tail:]}"
