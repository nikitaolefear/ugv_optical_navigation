# B2-LOG paced discovery tests

- Previous build and unchanged test sources, 29/29 PASS.
- Updated overlay, 34/34 PASS. Includes the 22 core/integration cases, seven equivalent discovery cases now using indexed requests and five new PWM/flood/transport/pacing tests.
- The parser and IPC checks used isolated imports, temporary sockets and synthetic MAVLink messages.
- Rollback mocks 3/3 PASS. Patch reproduction and SHA256 checked.
- No live request, reset, deployment, PWM change, write or transport fault reset.
