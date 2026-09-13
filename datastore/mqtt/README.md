# MQTT Datastore

This package implements an MQTT (Message Queuing Telemetry Transport) producer for the Fleet Telemetry system. MQTT is particularly well-suited for fleet telemetry systems due to its lightweight, publish-subscribe architecture.

## Overview

The MQTT datastore allows the Fleet Telemetry system to publish vehicle data, alerts, errors and connectivity to an MQTT broker. It uses the Paho MQTT client library for Go and implements the `telemetry.Producer` interface.

## Key Design Decisions

1. **Separate topics for different data types**: We use distinct topic structures for metrics, alerts, errors and connectivity to allow easy filtering and processing by subscribers.

2. **Individual field publishing**: Each metric field is published as a separate MQTT message, allowing for granular updates and subscriptions.

3. **Current state and history for alerts**: We maintain both the current state and history of alerts, supporting both clients that require real-time monitoring and clients that require historical analysis.

4. **Configurable QoS and retention**: The MQTT QoS level and message retention can be configured to balance between performance and reliability.

5. **Reliable acknowledgment support**: The producer supports reliable acknowledgment for specified transaction types. However, it's important to note that the entire packet from the vehicle will be not be acknowledged if any of the related MQTT publish operations fail. This ensures data integrity by preventing partial updates and allows for retrying the complete set of data in case of any publishing issues.

## Configuration

The MQTT producer is configured using a JSON object with the following fields:

- `broker`: (string) The MQTT broker "host:port". (for example "localhost:1883")
- `client_id`: (string) A unique identifier for the MQTT client.
- `username`: (string) The username for MQTT broker authentication. (optional)
- `password`: (string) The password for MQTT broker authentication. (optional)
- `topic_base`: (string) The base topic for all MQTT messages.
- `qos`: (number) The Quality of Service level (0, 1, or 2). Default: 0
- `retained`: (boolean) Whether messages should be retained by the broker. Default: false
- `publish_vehicle_records`: (boolean) Also publish complete vehicle record envelopes, preserving sample timestamps and typed values. Default: false. Record envelopes are never retained, even when `retained` is true.
- `connect_timeout_ms`: (number) Connection timeout in milliseconds. Default: 30000
- `publish_timeout_ms`: (number) Publish operation timeout in milliseconds. Default: 2500
- `disconnect_timeout_ms`: (number) Disconnection timeout in milliseconds. Default: 250
- `connect_retry_interval_ms`: (number) Interval between connection retry attempts in milliseconds. Default: 10000
- `keep_alive_seconds`: (number) Keep-alive interval in seconds. Default: 30

Example configuration:

```json
{
  "mqtt": {
    "broker": "localhost:1883",
    "client_id": "fleet-telemetry",
    "username": "your_username",
    "password": "your_password",
    "topic_base": "telemetry",
    "qos": 1,
    "retained": false,
    "connect_timeout_ms": 30000,
    "publish_timeout_ms": 2500,
    "disconnect_timeout_ms": 250,
    "connect_retry_interval_ms": 10000,
    "keep_alive_seconds": 30
  }
}
```

The MQTT producer will use default values for any omitted fields as specified above.

## Topic Structure

- Metrics: `<topic_base>/<VIN>/v/<field_name>`
- Vehicle records (opt-in): `<topic_base>/<VIN>/records`
- Alerts (current state): `<topic_base>/<VIN>/alerts/<alert_name>/current`
- Alerts (history): `<topic_base>/<VIN>/alerts/<alert_name>/history`
- Errors: `<topic_base>/<VIN>/errors/<error_name>`
- Connectivity: `<topic_base>/<VIN>/connectivity`

## Payload Formats

All payloads are JSON encoded. Please note that the metric field values are also JSON encoded.

- Metrics: `<field_value>`
- Alerts: `{"Name": <string>, "StartedAt": <timestamp>, "EndedAt": <timestamp>, "Audiences": [<string>]}`
- Errors: `{"Name": <string>, "Body": <string>, "Tags": {<string>: <string>}, "CreatedAt": <timestamp>}`
- Connectivity: `{"ConnectionId": <string>, "Status": <string>, "CreatedAt": <timestamp>}`

Note: The field contents and type are determined by the car. Fields may have their types updated with different software and vehicle versions to optimize for precision or space. For example, a float value like the vehicle's speed might be received as 12.3 (numeric) in one version and as "12.3" (string) in another version.

### Vehicle record envelopes

Set `publish_vehicle_records` to `true` to receive one additional message per vehicle data record. Existing metric topics and payloads remain unchanged. The new topic contains the Tesla `Payload` protobuf encoded as ProtoJSON using original protobuf field names and populated default fields:

```json
{
  "vin": "TEST123",
  "created_at": "2026-09-11T12:34:56.123456789Z",
  "is_resend": false,
  "data": [
    {"key": "VehicleSpeed", "value": {"double_value": 42}},
    {"key": "Location", "value": {"location_value": {"latitude": 0, "longitude": 0}}},
    {"key": "TimeToFullCharge", "value": {"invalid": true}}
  ]
}
```

The VIN comes from the authenticated record identity. The original `created_at` timestamp, including fractional seconds, and `is_resend` flag are preserved. Values retain their protobuf oneof types after the receiver's normal record transformations; invalid data remains an explicit `invalid` value. ProtoJSON represents 64-bit integers as strings and special floating-point values as strings. Consumers should tolerate new fields and value types.

Each record is a partial update: omitted fields are unchanged. Record envelopes are never retained because the most recent record is not a complete vehicle snapshot. Consumers that need restart recovery should persist their merged state and per-field source timestamps. Use `created_at` to reject older samples and deduplicate retransmissions; MQTT receipt time does not establish when the vehicle sampled a value. A missing timestamp is represented as `null`, not replaced with server receipt time.

Record publishing uses the configured QoS and participates in the same publish timeout and reliable acknowledgment checks as individual fields. A failed record or field publication prevents the vehicle record from being acknowledged. Reliable acknowledgment confirms broker publication, not that a downstream consumer has processed or stored the message.

## Error Handling and Reliability

- The producer implements reconnection logic with configurable retry intervals.
- Publish operations have a configurable timeout to prevent blocking indefinitely.
- The producer supports reliable acknowledgment for specified transaction types, ensuring critical data is not lost.

## Performance Considerations

- Each field is published as a separate MQTT message, which can increase network traffic but allows for more granular subscriptions.
- QoS levels can be configured to balance between performance and reliability.
- The producer uses goroutines to handle message publishing asynchronously.

### Optional location archive

`location_archive` accepts an absolute `directory` and a `vehicles` map from authenticated vehicle ID to unique safe alias. It is disabled when omitted. Every Location datum is appended and synced before MQTT publication and reliable ACK; failures withhold both. Source/receipt timestamps, resend flags and complete typed Location values are retained without VINs or unrelated fields. The caller owns backup, disk-capacity monitoring, access control, and retention. Files never auto-purge. Retries may create duplicate rows; source timestamps and resend metadata support downstream interpretation.
