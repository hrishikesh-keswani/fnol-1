-- SELECT on trace.trace_events is not enough. The reviewer must also
-- be allowed to use the trace schema, or the Gemma paragraph stays hidden.

GRANT USAGE ON SCHEMA {{trace}} TO fnol_reviewer;
