//! RabbitMQ plumbing — lapin binding for the `bss.events` topic exchange.
//!
//! Port of the aio-pika wiring the Python services share (declare the durable
//! `bss.events` topic exchange; publish persistent JSON; declare+bind a durable
//! queue and consume). lapin runs its own async-io reactor; the `tokio-*-trait`
//! shims host it on the tokio runtime so there's a single reactor (matching the
//! services' single-loop posture).
//!
//! This is the minimal surface Phase 1 (rating) needs: connect, `publish_json`
//! (the inline-publish path rating's consumer uses — no `message_id`, exactly
//! like `events/publisher.publish`), and `declare_and_bind` returning a raw
//! [`lapin::Consumer`] the service drives. The relay tick loop (only
//! subscription/com/som run it) lands in P2/P3 where a service exercises it.

use lapin::{
    options::{
        BasicConsumeOptions, BasicPublishOptions, BasicQosOptions, ExchangeDeclareOptions,
        QueueBindOptions, QueueDeclareOptions,
    },
    types::{AMQPValue, FieldTable, LongString, ShortString},
    BasicProperties, Channel, Connection, ConnectionProperties, Consumer, ExchangeKind,
};
use serde_json::Value;

use crate::topology::{parked_queue_name, retry_queue_name, EXCHANGE_NAME, RETRY_EXCHANGE_NAME};

/// Normalize the AMQP vhost to match aio-pika semantics: a URL ending in a bare
/// `/` carries an *empty* vhost, which lapin rejects (`vhost  not found`), but
/// aio-pika (and thus the running broker's config) treats it as the default
/// vhost `/`. Rewrite that one case to the URL-encoded default `%2f`; a URL with
/// an explicit vhost (`.../myvhost`) or none at all (`amqp://host`) is untouched.
fn normalize_vhost(mq_url: &str) -> String {
    if mq_url.ends_with('/') {
        format!("{mq_url}%2f")
    } else {
        mq_url.to_string()
    }
}

/// A live channel onto the `bss.events` topic exchange, with self-healing
/// reconnection.
///
/// lapin does **not** auto-recover a channel/connection that errors (unlike
/// aio-pika's robust connection the Python services use): a transient broker blip
/// leaves `self.channel` in a permanent error state, and every subsequent
/// `basic_publish` returns `invalid channel state`. That once wedged the outbox
/// relay for days (the P7 E2E incident). So the channel + connection live behind a
/// mutex and [`MqChannel::healthy_channel`] recreates them on demand.
///
/// Reconnection covers the **publish** path (the relay + inline publishes, which is
/// what wedged). A [`Consumer`] returned by [`declare_and_bind`]/[`bind_safe_consumer`]
/// is bound to the channel live at setup time; if that channel later dies the
/// consumer stream ends and the service's consume loop must re-invoke the bind to
/// re-subscribe (consumer-loop re-subscription is tracked separately).
///
/// [`declare_and_bind`]: MqChannel::declare_and_bind
/// [`bind_safe_consumer`]: MqChannel::bind_safe_consumer
pub struct MqChannel {
    mq_url: String,
    inner: tokio::sync::Mutex<Inner>,
}

/// The recoverable connection + channel pair, guarded by [`MqChannel::inner`].
struct Inner {
    connection: Connection,
    channel: Channel,
}

impl MqChannel {
    /// Connect to `mq_url`, open a channel, set prefetch to 5 (the services'
    /// `set_qos(prefetch_count=5)`), and declare the durable `bss.events` topic
    /// exchange.
    pub async fn connect(mq_url: &str) -> Result<Self, lapin::Error> {
        let connection = Connection::connect(&normalize_vhost(mq_url), Self::conn_props()).await?;
        let channel = Self::open_channel(&connection).await?;
        Ok(MqChannel {
            mq_url: mq_url.to_string(),
            inner: tokio::sync::Mutex::new(Inner {
                connection,
                channel,
            }),
        })
    }

    /// The tokio-hosted connection properties (rebuilt per connect/reconnect —
    /// `ConnectionProperties` isn't `Clone`).
    fn conn_props() -> ConnectionProperties {
        ConnectionProperties::default()
            .with_executor(tokio_executor_trait::Tokio::current())
            .with_reactor(tokio_reactor_trait::Tokio)
    }

    /// Open a fresh channel on `connection`: set prefetch to 5 and (idempotently)
    /// re-declare the durable `bss.events` topic exchange. Used at connect and on
    /// every reconnect.
    async fn open_channel(connection: &Connection) -> Result<Channel, lapin::Error> {
        let channel = connection.create_channel().await?;
        channel.basic_qos(5, BasicQosOptions::default()).await?;
        channel
            .exchange_declare(
                EXCHANGE_NAME,
                ExchangeKind::Topic,
                ExchangeDeclareOptions {
                    durable: true,
                    ..Default::default()
                },
                FieldTable::default(),
            )
            .await?;
        Ok(channel)
    }

    /// A connected channel, recreating it (and reconnecting the connection if it too
    /// dropped) when the current one has errored. Returns a cheap clone (lapin
    /// `Channel` is `Arc`-backed) so the caller publishes without holding the lock.
    /// The mutex serialises a reconnect so a burst of failing publishers triggers a
    /// single reconnect, not one per caller.
    async fn healthy_channel(&self) -> Result<Channel, lapin::Error> {
        let mut inner = self.inner.lock().await;
        if inner.channel.status().connected() {
            return Ok(inner.channel.clone());
        }
        // Channel errored — reconnect the connection first if it's down too.
        if !inner.connection.status().connected() {
            inner.connection =
                Connection::connect(&normalize_vhost(&self.mq_url), Self::conn_props()).await?;
        }
        let channel = Self::open_channel(&inner.connection).await?;
        inner.channel = channel.clone();
        tracing::info!("mq.channel.reconnected");
        Ok(channel)
    }

    /// Publish `payload` as a persistent JSON message with routing key
    /// `routing_key`. Mirrors the inline `events/publisher.publish` message:
    /// `content_type=application/json`, persistent delivery, no `message_id`.
    pub async fn publish_json(
        &self,
        routing_key: &str,
        payload: &Value,
    ) -> Result<(), lapin::Error> {
        let body = serde_json::to_vec(payload).unwrap_or_else(|_| b"{}".to_vec());
        self.healthy_channel()
            .await?
            .basic_publish(
                EXCHANGE_NAME,
                routing_key,
                BasicPublishOptions::default(),
                &body,
                BasicProperties::default()
                    .with_content_type("application/json".into())
                    .with_delivery_mode(2), // persistent
            )
            .await?
            .await?; // await the broker confirm/return
        Ok(())
    }

    /// Declare a durable `queue`, bind it to `routing_key` on `bss.events`, and
    /// start consuming. The caller drives the returned [`Consumer`] stream and
    /// acks each delivery (rating acks unconditionally — it catches its own
    /// handler errors, never requeues).
    pub async fn declare_and_bind(
        &self,
        queue: &str,
        routing_key: &str,
        consumer_tag: &str,
    ) -> Result<Consumer, lapin::Error> {
        let channel = self.healthy_channel().await?;
        channel
            .queue_declare(
                queue,
                QueueDeclareOptions {
                    durable: true,
                    ..Default::default()
                },
                FieldTable::default(),
            )
            .await?;
        channel
            .queue_bind(
                queue,
                EXCHANGE_NAME,
                routing_key,
                QueueBindOptions::default(),
                FieldTable::default(),
            )
            .await?;
        channel
            .basic_consume(
                queue,
                consumer_tag,
                BasicConsumeOptions::default(),
                FieldTable::default(),
            )
            .await
    }

    /// Publish `payload` with an explicit AMQP `message_id` — the relay path (the
    /// inbox dedups consumers on this id). Otherwise identical to
    /// [`MqChannel::publish_json`].
    pub async fn publish_json_with_id(
        &self,
        routing_key: &str,
        payload: &Value,
        message_id: &str,
    ) -> Result<(), lapin::Error> {
        let body = serde_json::to_vec(payload).unwrap_or_else(|_| b"{}".to_vec());
        self.publish_bytes_with_id(routing_key, &body, message_id)
            .await
    }

    /// Publish pre-serialized `body` bytes with an AMQP `message_id` — the relay's
    /// drain path (it already serialized the payload).
    pub async fn publish_bytes_with_id(
        &self,
        routing_key: &str,
        body: &[u8],
        message_id: &str,
    ) -> Result<(), lapin::Error> {
        self.healthy_channel()
            .await?
            .basic_publish(
                EXCHANGE_NAME,
                routing_key,
                BasicPublishOptions::default(),
                body,
                BasicProperties::default()
                    .with_content_type("application/json".into())
                    .with_delivery_mode(2)
                    .with_message_id(ShortString::from(message_id)),
            )
            .await?
            .await?;
        Ok(())
    }

    /// Declare the shared retry (dead-letter) exchange — a durable direct
    /// exchange. Idempotent (port of `declare_retry_exchange`).
    pub async fn declare_retry_exchange(&self) -> Result<(), lapin::Error> {
        self.healthy_channel()
            .await?
            .exchange_declare(
                RETRY_EXCHANGE_NAME,
                ExchangeKind::Direct,
                ExchangeDeclareOptions {
                    durable: true,
                    ..Default::default()
                },
                FieldTable::default(),
            )
            .await
    }

    /// Declare the main + retry + parked topology for `queue_name` and start
    /// consuming the main queue — the lapin half of `bind_consumer`. The main
    /// queue dead-letters to the retry exchange (keyed by its own name); the retry
    /// queue holds the message for `retry_backoff_ms` then dead-letters it back to
    /// the main exchange under `routing_key`; the parked queue is the terminal
    /// resting place. Arg *types* mirror aio-pika (TTL as an integer, DLX names as
    /// strings) so a Rust and a Python service can share the durable queues.
    pub async fn bind_safe_consumer(
        &self,
        queue_name: &str,
        routing_key: &str,
        consumer_tag: &str,
        retry_backoff_ms: u64,
    ) -> Result<Consumer, lapin::Error> {
        let channel = self.healthy_channel().await?;

        // Main queue → retry exchange on failure.
        let mut main_args = FieldTable::default();
        main_args.insert(
            "x-dead-letter-exchange".into(),
            AMQPValue::LongString(LongString::from(RETRY_EXCHANGE_NAME)),
        );
        main_args.insert(
            "x-dead-letter-routing-key".into(),
            AMQPValue::LongString(LongString::from(queue_name)),
        );
        channel
            .queue_declare(
                queue_name,
                QueueDeclareOptions {
                    durable: true,
                    ..Default::default()
                },
                main_args,
            )
            .await?;
        channel
            .queue_bind(
                queue_name,
                EXCHANGE_NAME,
                routing_key,
                QueueBindOptions::default(),
                FieldTable::default(),
            )
            .await?;

        // Retry queue: TTL then dead-letter back to the main exchange/routing key.
        let retry_q = retry_queue_name(queue_name);
        let mut retry_args = FieldTable::default();
        retry_args.insert(
            "x-message-ttl".into(),
            AMQPValue::LongLongInt(retry_backoff_ms as i64),
        );
        retry_args.insert(
            "x-dead-letter-exchange".into(),
            AMQPValue::LongString(LongString::from(EXCHANGE_NAME)),
        );
        retry_args.insert(
            "x-dead-letter-routing-key".into(),
            AMQPValue::LongString(LongString::from(routing_key)),
        );
        channel
            .queue_declare(
                &retry_q,
                QueueDeclareOptions {
                    durable: true,
                    ..Default::default()
                },
                retry_args,
            )
            .await?;
        channel
            .queue_bind(
                &retry_q,
                RETRY_EXCHANGE_NAME,
                queue_name,
                QueueBindOptions::default(),
                FieldTable::default(),
            )
            .await?;

        // Parked queue: terminal resting place for poison messages.
        channel
            .queue_declare(
                &parked_queue_name(queue_name),
                QueueDeclareOptions {
                    durable: true,
                    ..Default::default()
                },
                FieldTable::default(),
            )
            .await?;

        channel
            .basic_consume(
                queue_name,
                consumer_tag,
                BasicConsumeOptions::default(),
                FieldTable::default(),
            )
            .await
    }

    /// Park a poison message: publish it to `<queue_name>.parked` via the default
    /// exchange, carrying the failure reason. Mirrors the Python park publish.
    pub async fn publish_parked(
        &self,
        queue_name: &str,
        body: &[u8],
        message_id: Option<&str>,
        reason: &str,
    ) -> Result<(), lapin::Error> {
        let parked = parked_queue_name(queue_name);
        let mut headers = FieldTable::default();
        headers.insert(
            "parked_reason".into(),
            AMQPValue::LongString(LongString::from(truncate(reason, 500))),
        );
        let mut props = BasicProperties::default()
            .with_content_type("application/json".into())
            .with_delivery_mode(2)
            .with_headers(headers);
        if let Some(id) = message_id {
            props = props.with_message_id(ShortString::from(id));
        }
        // Default (nameless) exchange routes by queue name.
        self.healthy_channel()
            .await?
            .basic_publish("", &parked, BasicPublishOptions::default(), body, props)
            .await?
            .await?;
        Ok(())
    }
}

fn truncate(s: &str, max: usize) -> String {
    s.chars().take(max).collect()
}
