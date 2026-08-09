# syntax=docker/dockerfile:1

# ---------- Stage 1: build ---------------------------------------------------
FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /build

# The pom is copied on its own, before the source. Docker caches each instruction
# as a layer, so editing a .java file invalidates only the layers below this point
# and dependency resolution is skipped entirely.
COPY pom.xml .

# --mount=type=cache gives this RUN a directory that survives between builds and
# is shared by all three services. Spring Boot's dependency tree is downloaded
# once for the whole project rather than once per service.
RUN --mount=type=cache,target=/root/.m2 \
    mvn -B -q dependency:go-offline

COPY src ./src
RUN --mount=type=cache,target=/root/.m2 \
    mvn -B -q clean package -DskipTests

# ---------- Stage 2: runtime -------------------------------------------------
# Only the jar crosses this line. No Maven, no compiler, no source: 71 MB base
# instead of 234 MB, and a far smaller attack surface.
FROM eclipse-temurin:21-jre-alpine
WORKDIR /app

RUN addgroup -S app && adduser -S app -G app

COPY --from=build /build/target/app.jar app.jar
USER app

EXPOSE 8080

# Note what is NOT here: no -javaagent flag. The OpenTelemetry agent is attached
# purely through JAVA_TOOL_OPTIONS in docker-compose.yml, so instrumentation
# requires no change to the code AND no change to the image.
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
