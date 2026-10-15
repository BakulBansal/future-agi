from typing import Optional

from django.db import connection


class SQL_query_handler:
    @classmethod
    def execute_query(cls, query, params=None):
        """Generic method to execute SQL queries and return results"""

        with connection.cursor() as cursor:
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            rows = cursor.fetchall()
        return rows

    @classmethod
    def evals_insight_query(cls, project_version_id):
        """Returns evaluation insights data for a project version."""
        query = """
        WITH base AS (
            SELECT
                logger.custom_eval_config_id,
                config.name,
                MIN(template.choices::text)::jsonb AS choices,
                COUNT(logger.id) AS total_count,
                AVG(logger.output_float) AS avg_float_score,
                AVG(CASE WHEN NOT logger.output_bool THEN 1 ELSE NULL END) AS avg_bool_fail_score,
                AVG(CASE WHEN logger.output_bool THEN 1 ELSE NULL END) AS avg_bool_pass_score
            FROM tracer_eval_logger logger
            LEFT JOIN tracer_custom_eval_config config ON logger.custom_eval_config_id = config.id
            LEFT JOIN model_hub_evaltemplate template ON config.eval_template_id = template.id
            WHERE NOT logger.deleted
                AND logger.trace_id IN (
                    SELECT id
                    FROM tracer_trace
                    WHERE NOT deleted
                        AND project_version_id = %s
                )
            GROUP BY
                logger.custom_eval_config_id,
                config.name
        ),

        str_list_scores AS (
            SELECT
                logger.custom_eval_config_id,
                choice.value AS label,
                ROUND(100.0 * COUNT(*)::numeric / NULLIF(total.total_count, 0), 2) AS score
            FROM tracer_eval_logger logger
            LEFT JOIN tracer_custom_eval_config config ON logger.custom_eval_config_id = config.id
            LEFT JOIN model_hub_evaltemplate template ON config.eval_template_id = template.id
            LEFT JOIN LATERAL jsonb_array_elements_text(template.choices) AS choice(value) ON TRUE
            JOIN (
                SELECT
                    custom_eval_config_id,
                    COUNT(*) AS total_count
                FROM tracer_eval_logger
                WHERE NOT deleted AND (error IS NULL OR error = FALSE)
                  AND trace_id IN (
                      SELECT id
                      FROM tracer_trace
                      WHERE NOT deleted
                        AND project_version_id = %s
                  )
                GROUP BY custom_eval_config_id
            ) total ON total.custom_eval_config_id = logger.custom_eval_config_id
            WHERE NOT logger.deleted
                AND logger.output_str_list ? choice.value
                AND logger.trace_id IN (
                    SELECT id
                    FROM tracer_trace
                    WHERE NOT deleted
                        AND project_version_id = %s
                )
            GROUP BY
                logger.custom_eval_config_id,
                choice.value,
                total.total_count
        ),

        aggregated AS (
            SELECT
                custom_eval_config_id,
                jsonb_object_agg(label, jsonb_build_object('score', score)) AS str_list_score
            FROM str_list_scores
            GROUP BY custom_eval_config_id
        ),

        errors AS (
            SELECT
                logger.custom_eval_config_id,
                COUNT(*) AS total_errors_count,
                jsonb_agg(DISTINCT logger.trace_id) AS failed_trace_ids
            FROM tracer_eval_logger logger
            WHERE NOT logger.deleted
                AND (logger.error = TRUE OR logger.output_str = 'ERROR')
                AND logger.trace_id IN (
                    SELECT id
                    FROM tracer_trace
                    WHERE NOT deleted
                        AND project_version_id = %s
                )
            GROUP BY logger.custom_eval_config_id
        )

        SELECT
            base.*,
            aggregated.str_list_score::jsonb,
            COALESCE(errors.total_errors_count, 0) AS total_errors_count,
            COALESCE(errors.failed_trace_ids, '[]'::jsonb) AS failed_trace_ids
        FROM base
        LEFT JOIN aggregated ON base.custom_eval_config_id = aggregated.custom_eval_config_id
        LEFT JOIN errors ON base.custom_eval_config_id = errors.custom_eval_config_id
        """

        return cls.execute_query(
            query,
            (
                project_version_id,
                project_version_id,
                project_version_id,
                project_version_id,
            ),
        )

    @classmethod
    def fetch_children_ids_query(cls, observation_span_id):
        """Returns child span IDs for a given observation span (recursive)."""
        query = """
        WITH RECURSIVE span_tree AS (
            SELECT id, parent_span_id, 0 as level
            FROM tracer_observation_span
            WHERE id = %s

            UNION ALL

            SELECT s.id, s.parent_span_id, st.level + 1
            FROM tracer_observation_span s
            INNER JOIN span_tree st ON s.parent_span_id = st.id
        )
        SELECT id FROM span_tree WHERE level > 0
        """

        return cls.execute_query(query, (observation_span_id,))

    @classmethod
    def fetch_children_query(cls, observation_span_id):
        """Returns children spans for a given observation span ID, ordered by start_time."""
        query = """
        WITH RECURSIVE span_tree AS (
            SELECT id, parent_span_id, name, observation_type, prompt_tokens, total_tokens, latency_ms, completion_tokens, span_events, trace_id, cost, start_time, 0 as level,
                ARRAY[start_time] as sort_path
            FROM tracer_observation_span
            WHERE id = %s

            UNION ALL

            SELECT s.id, s.parent_span_id, s.name, s.observation_type, s.prompt_tokens, s.total_tokens, s.latency_ms, s.completion_tokens, s.span_events, s.trace_id, s.cost, s.start_time, st.level + 1,
                st.sort_path || s.start_time
            FROM tracer_observation_span s
            INNER JOIN span_tree st ON s.parent_span_id = st.id
        )
        SELECT id, parent_span_id, name, observation_type, prompt_tokens, total_tokens, latency_ms, completion_tokens, span_events, trace_id, cost
        FROM span_tree
        WHERE level > 0
        ORDER BY sort_path
        """

        return cls.execute_query(query, (observation_span_id,))

    @classmethod
    def get_error_clusters_for_feed(cls, cutoff_date, project_ids, limit, offset=0):
        """
        Get error clusters for the feed view across multiple projects.
        Returns clusters with user counts, event counts, and other metrics.

        Args:
            cutoff_date: datetime - earliest date to include
            project_ids: list of project IDs to filter by (can be empty list)
            limit: max number of results
            offset: pagination offset
        """

        # No accessible projects - return empty results without querying
        if not project_ids:
            return []

        # Build dynamic WHERE clause for project filtering
        placeholders = ",".join(["%s"] * len(project_ids))
        project_filter = f"AND teg.project_id IN ({placeholders})"
        params = [cutoff_date] + list(project_ids) + [limit, offset]

        # Query directly from TraceErrorGroup which has all the aggregated data
        query = f"""
            SELECT
                teg.cluster_id,
                teg.error_type,
                teg.combined_impact,
                teg.combined_description,
                teg.total_events,
                teg.unique_traces,
                teg.last_seen,
                teg.first_seen,
                teg.project_id,
                p.name as project_name,
                teg.unique_users,
                teg.assignee_id
            FROM tracer_trace_error_group teg
            INNER JOIN tracer_project p ON teg.project_id = p.id
            WHERE teg.first_seen >= %s
            AND teg.deleted = FALSE
            {project_filter}
            ORDER BY
                CASE teg.combined_impact
                    WHEN 'HIGH' THEN 1
                    WHEN 'MEDIUM' THEN 2
                    WHEN 'LOW' THEN 3
                    ELSE 4
                END,
                teg.last_seen DESC
            LIMIT %s OFFSET %s
        """

        return cls.execute_query(query, params)

    @classmethod
    def get_error_clusters_count(cls, cutoff_date, project_ids):
        """
        Get total count of error clusters for pagination.

        Args:
            cutoff_date: datetime - earliest date to include
            project_ids: list of project IDs to filter by (can be empty list)
        """

        # No accessible projects - return zero without querying
        if not project_ids:
            return 0

        # Build dynamic WHERE clause for project filtering
        placeholders = ",".join(["%s"] * len(project_ids))
        project_filter = f"AND teg.project_id IN ({placeholders})"
        params = [cutoff_date] + list(project_ids)

        query = f"""
            SELECT COUNT(DISTINCT teg.cluster_id) as total_count
            FROM tracer_trace_error_group teg
            WHERE teg.first_seen >= %s
            AND teg.deleted = FALSE
            {project_filter}
        """

        result = cls.execute_query(query, params)
        return result[0][0] if result else 0

    @classmethod
    def get_span_attributes_for_project(cls, project_id):
        """
        Returns a list of distinct span_attributes keys for a given project using raw SQL.

        Uses COALESCE to check span_attributes first, falling back to eval_attributes
        for backward compatibility during migration.
        """
        query = """
            SELECT DISTINCT key
            FROM tracer_observation_span,
            LATERAL jsonb_object_keys(
                COALESCE(NULLIF(span_attributes, '{}'::jsonb), eval_attributes)
            ) AS key
            WHERE project_id = %s
              AND (span_attributes IS NOT NULL AND span_attributes != '{}'::jsonb
                   OR eval_attributes IS NOT NULL AND eval_attributes != '{}'::jsonb)
        """
        rows = cls.execute_query(query, [project_id])
        return [row[0] for row in rows]

    @classmethod
    def get_eval_attributes_for_project(cls, project_id):
        """
        DEPRECATED: Use get_span_attributes_for_project instead.

        Returns a list of distinct eval_attributes keys for a given project using raw SQL.
        This method now delegates to get_span_attributes_for_project for backward compatibility.
        """
        return cls.get_span_attributes_for_project(project_id)

    @classmethod
    def get_system_prompt_from_traces(
        cls, project_id: str, trace_ids: list[str]
    ) -> Optional[str]:
        """
        Extract the system prompt from spans in the given traces.

        Searches for llm.input_messages.{i}.message.role == "system" and returns
        the corresponding llm.input_messages.{i}.message.content.

        Uses COALESCE to check span_attributes first, falling back to eval_attributes
        for backward compatibility during migration.

        Returns the first system prompt found, or None if not found.
        """
        if not trace_ids:
            return None

        list_of_trace_ids = ",".join(["%s"] * len(trace_ids))

        # Use COALESCE to prefer span_attributes, fall back to eval_attributes
        query = f"""
            WITH combined_attrs AS (
                SELECT
                    id,
                    COALESCE(span_attributes, eval_attributes) AS attributes
                FROM tracer_observation_span
                WHERE project_id = %s
                  AND trace_id IN ({list_of_trace_ids})
                  AND observation_type = 'llm'
                  AND (span_attributes IS NOT NULL OR eval_attributes IS NOT NULL)
                  AND deleted = FALSE
            )
            SELECT
                attributes->>regexp_replace(attribute.key, 'role$', 'content') AS system_prompt
            FROM combined_attrs,
            LATERAL jsonb_each_text(attributes) AS attribute
            WHERE attribute.key ~ '^llm\\.input_messages\\.\\d+\\.message\\.role$'
              AND attribute.value = 'system'
            LIMIT 1
        """

        params = [project_id] + (trace_ids)
        rows = cls.execute_query(query, params)

        return rows[0][0] if rows else None
